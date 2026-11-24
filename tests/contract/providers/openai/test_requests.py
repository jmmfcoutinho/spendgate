"""Request side: allowlists, forced fields, URL and auth, and the include_usage strip."""

import json
from collections.abc import Mapping
from dataclasses import replace

import pytest

from spendgate.core.errors import ErrorCode, Refusal
from spendgate.providers import openai_shapes as shapes
from spendgate.providers.openai import OpenAIProvider, endpoint_url
from spendgate.providers.port import CleanRequest, Deployment, Json, SseEvent, Untranslatable
from spendgate.providers.sse import serialise
from tests.contract.providers.ceiling import DEFAULT_CEILING, ceiling_of
from tests.contract.providers.openai.conftest import SECRET
from tests.unit.audio_builders import wav

MSG: Json = [{"role": "user", "content": "hi"}]
CHAT = shapes.CHAT


def sent_body(provider: OpenAIProvider, req: CleanRequest, dep: Deployment) -> dict[str, Json]:
    body = json.loads(provider.translate(req, dep).body)
    assert isinstance(body, dict)
    return body


def validate(
    endpoint: str,
    body: Mapping[str, Json],
    files: Mapping[str, bytes] | None = None,
    *,
    remote_ok: bool = False,
) -> Refusal | None:
    req = CleanRequest(endpoint, "m", body, files=files or {})
    return OpenAIProvider(remote_ok=remote_ok).validate(req)


def chat(**extra: Json) -> CleanRequest:
    body: dict[str, Json] = {"model": "m", "messages": MSG, **extra}
    return CleanRequest(
        CHAT, "m", body, stream=body.get("stream") is True, output_ceiling=ceiling_of(body)
    )


def test_unknown_body_fields_are_refused_naming_the_param() -> None:
    cases: list[tuple[str, dict[str, Json]]] = [
        (CHAT, {"messages": MSG, "surprise": 1}),
        (shapes.RESPONSES, {"input": "x", "surprise": 1}),
        (shapes.EMBEDDINGS, {"input": "x", "surprise": 1}),
        (shapes.SPEECH, {"input": "x", "voice": "alloy", "surprise": 1}),
    ]
    for endpoint, body in cases:
        refusal = validate(endpoint, body)
        assert refusal is not None
        assert refusal.code is ErrorCode.INVALID_REQUEST
        assert refusal.param == "surprise"


def test_unknown_transcription_parts_and_fields_are_refused() -> None:
    audio = {"file": wav(1.0)}
    refusal = validate(shapes.TRANSCRIPTIONS, {"stream": True}, audio)
    assert refusal is not None and refusal.param == "stream"
    refusal = validate(shapes.TRANSCRIPTIONS, {}, {"file": wav(1.0), "extra": b"x"})
    assert refusal is not None and refusal.param == "file"
    assert validate(shapes.TRANSCRIPTIONS, {"language": "en"}, audio) is None


@pytest.mark.parametrize("tier", ["priority", "flex", "scale", "fast", "auto", "default"])
def test_service_tier_is_refused_on_every_endpoint_that_could_carry_it(tier: str) -> None:
    cases: list[tuple[str, dict[str, Json]]] = [
        (CHAT, {"messages": MSG, "service_tier": tier}),
        (shapes.RESPONSES, {"input": "x", "service_tier": tier}),
    ]
    for endpoint, body in cases:
        refusal = validate(endpoint, body)
        assert refusal is not None and refusal.param == "service_tier"
        assert "price" in refusal.message


@pytest.mark.parametrize(
    "field", ["prompt_cache_retention", "prediction", "web_search_options", "audio", "metadata"]
)
def test_other_price_changing_or_hosted_chat_options_are_refused(field: str) -> None:
    refusal = validate(CHAT, {"messages": MSG, field: {}})
    assert refusal is not None and refusal.param == field


@pytest.mark.parametrize(
    ("body", "param"),
    [
        ({"input": "x", "previous_response_id": "r"}, "previous_response_id"),
        ({"input": "x", "background": True}, "background"),
        ({"input": "x", "store": True}, "store"),
        ({"input": "x", "stream_options": {"include_usage": True}}, "stream_options"),
        ({"input": "x", "tools": [{"type": "web_search"}]}, "tools[0].type"),
        ({"input": "x", "include": ["file_search_call.results"]}, "include"),
        ({"input": [{"type": "item_reference", "id": "i"}]}, "input[0].type"),
        (
            {
                "input": [
                    {"role": "user", "content": [{"type": "input_image", "image_url": "http://x"}]}
                ]
            },
            "input[0].content[0].image_url",
        ),
    ],
)
def test_responses_state_hosted_tools_and_remote_media_are_refused(
    body: Mapping[str, Json], param: str
) -> None:
    refusal = validate(shapes.RESPONSES, body)
    assert refusal is not None and refusal.param == param


@pytest.mark.parametrize(
    ("extra", "param"),
    [
        ({"n": 2}, "n"),
        ({"store": True}, "store"),
        ({"modalities": ["text", "audio"]}, "modalities"),
        ({"functions": []}, "functions"),
        ({"stream_options": {"include_usage": True}}, "stream_options"),
        (
            {"stream": True, "stream_options": {"include_obfuscation": True}},
            "stream_options.include_obfuscation",
        ),
        (
            {"tools": [{"type": "function", "function": {"name": "f"}, "extra": 1}]},
            "tools[0].extra",
        ),
        ({"response_format": {"type": "xml"}}, "response_format.type"),
    ],
)
def test_chat_options_outside_the_allowlist_are_refused(
    extra: Mapping[str, Json], param: str
) -> None:
    refusal = validate(CHAT, {"messages": MSG, **extra})
    assert refusal is not None and refusal.param == param


def test_chat_content_parts_allow_text_and_data_urls_only() -> None:
    def parts(*p: Json) -> Mapping[str, Json]:
        return {"messages": [{"role": "user", "content": list(p)}]}

    image: Json = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    assert validate(CHAT, parts({"type": "text", "text": "x"}, image)) is None
    remote: Json = {"type": "image_url", "image_url": {"url": "https://evil.test/x.png"}}
    refusal = validate(CHAT, parts(remote))
    assert refusal is not None and (refusal.param or "").endswith("image_url.url")
    assert validate(CHAT, parts(remote), remote_ok=True) is None
    for kind in ("input_audio", "file"):
        refusal = validate(CHAT, parts({"type": kind}))
        assert refusal is not None and (refusal.param or "").endswith(".type")


def test_speech_limits_and_embedding_shapes() -> None:
    assert validate(shapes.SPEECH, {"input": "x" * 4097, "voice": "alloy"}) is not None
    assert validate(shapes.SPEECH, {"input": "x", "voice": "../evil"}) is not None
    sse = validate(shapes.SPEECH, {"input": "x", "voice": "alloy", "stream_format": "sse"})
    assert sse is not None and sse.param == "stream_format"
    assert validate(shapes.EMBEDDINGS, {"input": []}) is not None
    assert validate(shapes.EMBEDDINGS, {"input": ["a", [1, 2]], "dimensions": 8}) is None


def test_translate_rebuilds_the_body_and_forces_store_false(
    provider: OpenAIProvider, deployment: Deployment
) -> None:
    body = sent_body(provider, chat(temperature=0.2), deployment)
    assert body == {
        "model": "gpt-upstream", "messages": MSG, "temperature": 0.2, "store": False,
        "max_completion_tokens": DEFAULT_CEILING,
    }  # fmt: skip
    resp = CleanRequest(shapes.RESPONSES, "m", {"model": "m", "input": "hi"}, output_ceiling=64)
    assert sent_body(provider, resp, deployment)["store"] is False
    resp = CleanRequest(shapes.RESPONSES, "m", {"input": "hi", "store": False}, output_ceiling=64)
    assert sent_body(provider, resp, deployment)["store"] is False


def test_translate_refuses_what_validate_refuses_even_if_the_pipeline_skipped_it(
    provider: OpenAIProvider, deployment: Deployment
) -> None:
    with pytest.raises(Untranslatable) as caught:
        provider.translate(chat(service_tier="priority"), deployment)
    assert caught.value.refusal.param == "service_tier"


def test_include_usage_is_forced_on_chat_streams_and_the_injected_chunk_is_stripped(
    provider: OpenAIProvider, deployment: Deployment
) -> None:
    req = chat(stream=True)
    assert sent_body(provider, req, deployment)["stream_options"] == {"include_usage": True}
    assert provider.translate(req, deployment).stream is True
    usage_chunk = SseEvent(
        None, json.dumps({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    )
    text_chunk = SseEvent(
        None, json.dumps({"choices": [{"delta": {"content": "a"}}], "usage": None})
    )
    relay = provider.relay(req)
    assert relay.feed(serialise(text_chunk)) == serialise(text_chunk)
    assert relay.feed(serialise(usage_chunk)) == b""  # the usage-only chunk comes last
    assert relay.feed(serialise(SseEvent(None, "[DONE]"))) == b"data: [DONE]\n\n"
    assert relay.usage() is not None  # stripped from the client, still read for settlement


def test_include_usage_the_client_asked_for_passes_through_unchanged(
    provider: OpenAIProvider, deployment: Deployment
) -> None:
    req = chat(stream=True, stream_options={"include_usage": True})
    assert sent_body(provider, req, deployment)["stream_options"] == {"include_usage": True}
    usage_chunk = SseEvent(
        None, json.dumps({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    )
    assert provider.relay(req).feed(serialise(usage_chunk)) == serialise(usage_chunk)
    # A non-streamed chat call has nothing to strip either.
    assert provider.relay(chat()).feed(serialise(usage_chunk)) == serialise(usage_chunk)


def test_url_is_config_base_plus_fixed_path_and_auth_is_a_bearer_key(
    provider: OpenAIProvider, deployment: Deployment
) -> None:
    sent = provider.translate(chat(), deployment)
    assert sent.url == "https://upstream.test/v1/chat/completions"
    assert sent.headers["Authorization"] == f"Bearer {SECRET}"
    assert shapes_url(deployment, shapes.EMBEDDINGS) == "https://upstream.test/v1/embeddings"
    versioned = Deployment("openai", "https://api.example/v1/", "m", credential=SECRET)
    assert provider.translate(chat(), versioned).url == "https://api.example/v1/chat/completions"


def shapes_url(deployment: Deployment, endpoint: str) -> str:
    return endpoint_url(deployment.base_url, endpoint)


def test_a_client_cannot_steer_the_url_or_the_credential(
    provider: OpenAIProvider, deployment: Deployment
) -> None:
    req = CleanRequest(
        CHAT,
        "m",
        {"messages": MSG, "base_url": "https://evil.test"},
        headers={"Authorization": "Bearer client", "Host": "evil.test"},
    )
    with pytest.raises(Untranslatable):
        provider.translate(req, deployment)
    clean = chat()
    sent = provider.translate(
        replace(clean, headers={"Authorization": "Bearer client"}), deployment
    )
    assert "client" not in sent.headers["Authorization"]
    assert sent.url.startswith(deployment.base_url)


def test_transcription_is_rebuilt_as_multipart_with_a_container_extension(
    provider: OpenAIProvider, deployment: Deployment
) -> None:
    req = CleanRequest(
        shapes.TRANSCRIPTIONS,
        "m",
        {"language": "en", "timestamp_granularities[]": ["word", "segment"]},
        files={"file": wav(1.0)},
    )
    sent = provider.translate(req, deployment)
    ctype = sent.headers["Content-Type"]
    assert ctype.startswith("multipart/form-data; boundary=")
    assert sent.url.endswith("/v1/audio/transcriptions")
    assert b'name="model"\r\n\r\ngpt-upstream' in sent.body
    assert sent.body.count(b'name="timestamp_granularities[]"') == 2
    assert b'filename="audio.wav"' in sent.body
    unreadable = CleanRequest(shapes.TRANSCRIPTIONS, "m", {}, files={"file": b"not audio at all"})
    with pytest.raises(Untranslatable) as caught:
        provider.translate(unreadable, deployment)
    assert caught.value.refusal.param == "file"


def test_a_deployment_without_a_credential_fails_closed(provider: OpenAIProvider) -> None:
    with pytest.raises(ValueError, match="credential"):
        provider.translate(chat(), Deployment("openai", "https://x.test", "m"))
