"""OpenAI chat to Gemini: translation, refusals, both auth modes, URLs and secrets."""

import json
import logging
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from spendgate.providers.google import GoogleProvider, _service_account
from spendgate.providers.port import AuthMode, Deployment, Json, Untranslatable
from tests.contract.providers.google.helpers import (
    API_KEY,
    TOKEN,
    USER,
    FakeCredentials,
    dig,
    gemini_deployment,
    provider,
    request,
    sent,
    vertex_deployment,
)

PNG = "data:image/png;base64,iVBORw0KGgo="


def body_of(body: dict[str, object], *, stream: bool = False) -> dict[str, Json]:
    up = provider().translate(request(body, stream=stream), gemini_deployment())
    return sent(up.body)


# --- translation ----------------------------------------------------------------------------


def test_messages_become_contents_with_system_instruction_and_generation_config() -> None:
    out = body_of(
        {
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "developer", "content": "no emoji"},
                USER,
                {"role": "assistant", "content": "hello"},
            ],
            "max_tokens": 50,
            "max_completion_tokens": 40,
            "temperature": 0.2,
            "top_p": 0.9,
            "stop": "END",
        }
    )
    assert dig(out, "systemInstruction", "parts") == [{"text": "be brief"}, {"text": "no emoji"}]
    assert out["contents"] == [
        {"role": "user", "parts": [{"text": "hi"}]},
        {"role": "model", "parts": [{"text": "hello"}]},
    ]
    assert out["generationConfig"] == {
        "maxOutputTokens": 40,  # both ceilings set: the smaller applies
        "temperature": 0.2,
        "topP": 0.9,
        "stopSequences": ["END"],
    }


def test_data_url_images_become_inline_data_and_remote_urls_are_refused() -> None:
    content = [{"type": "text", "text": "see"}, {"type": "image_url", "image_url": {"url": PNG}}]
    out = body_of({"messages": [{"role": "user", "content": content}]})
    assert dig(out, "contents", 0, "parts", 1) == {
        "inlineData": {"mimeType": "image/png", "data": "iVBORw0KGgo="}
    }
    remote = [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}]
    with pytest.raises(Untranslatable) as refused:
        body_of({"messages": [{"role": "user", "content": remote}]})
    assert refused.value.refusal.param == "messages[0].content[0]"


@pytest.mark.parametrize(
    "url",
    [
        "data:video/mp4;base64,AAAA",
        "data:application/pdf;base64,AAAA",
        "data:audio/wav;base64,AAAA",
        "data:image/svg+xml;base64,AAAA",
        "DATA:image/png;base64,AAAA",
        "data:image/gif;base64,AAAA",  # not a Gemini image type
        "data:image/png;base64,AA\nAA",  # base64 on one line only, as in every adapter
    ],
)
def test_m14_only_image_data_urls_become_inline_data(url: str) -> None:
    content = [{"type": "image_url", "image_url": {"url": url}}]
    with pytest.raises(Untranslatable) as refused:
        body_of({"messages": [{"role": "user", "content": content}]})
    assert refused.value.refusal.param == "messages[0].content[0]"


def test_tools_tool_choice_and_tool_round_trip_translate_to_function_calling() -> None:
    call = {"id": "c1", "type": "function", "function": {"name": "w", "arguments": '{"q":"x"}'}}
    call["extra_content"] = {"google": {"thought_signature": "sig"}}
    out = body_of(
        {
            "messages": [
                USER,
                {"role": "assistant", "content": None, "tool_calls": [call]},
                {"role": "tool", "tool_call_id": "c1", "content": '{"temp": 3}'},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "w", "description": "d", "parameters": {"type": "object"}},
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "w"}},
        }
    )
    assert dig(out, "tools", 0, "functionDeclarations") == [
        {"name": "w", "description": "d", "parametersJsonSchema": {"type": "object"}}
    ]
    assert dig(out, "toolConfig") == {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["w"]}
    }
    assert dig(out, "contents", 1, "parts") == [
        {"functionCall": {"name": "w", "args": {"q": "x"}}, "thoughtSignature": "sig"}
    ]
    assert dig(out, "contents", 2) == {
        "role": "user",
        "parts": [{"functionResponse": {"name": "w", "response": {"temp": 3}}}],
    }


@pytest.mark.parametrize(
    ("choice", "mode"), [("auto", "AUTO"), ("none", "NONE"), ("required", "ANY")]
)
def test_string_tool_choices_map_to_function_calling_modes(choice: str, mode: str) -> None:
    out = body_of({"messages": [USER], "tool_choice": choice})
    assert dig(out, "toolConfig", "functionCallingConfig", "mode") == mode


def test_json_schema_response_format_becomes_response_schema() -> None:
    fmt = {"type": "json_schema", "json_schema": {"name": "n", "schema": {"type": "object"}}}
    out = body_of({"messages": [USER], "response_format": fmt})
    config = out["generationConfig"]
    assert dig(config, "responseMimeType") == "application/json"
    assert dig(config, "responseJsonSchema") == {"type": "object"}


def test_translate_rebuilds_the_body_and_never_forwards_user_or_model_fields() -> None:
    out = body_of({"messages": [USER], "user": "u-1", "model": "gateway-name", "store": False})
    assert set(out) == {"contents", "generationConfig"}  # attribution stays in the gateway


# --- refusals (never silently dropped) -----------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "seed",
        "n",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "presence_penalty",
        "frequency_penalty",
        "reasoning_effort",
        "verbosity",
        "parallel_tool_calls",
        "service_tier",
        "audio",
        "web_search_options",
    ],
)
def test_untranslatable_chat_fields_are_refused_naming_the_param(field: str) -> None:
    with pytest.raises(Untranslatable) as refused:
        body_of({"messages": [USER], field: 1})
    assert refused.value.refusal.param == field
    assert refused.value.refusal.code.value == "invalid_request"


@pytest.mark.parametrize(
    ("body", "param"),
    [
        ({"messages": [USER], "response_format": {"type": "json_object"}}, "response_format"),
        ({"messages": [USER], "tool_choice": {"type": "custom"}}, "tool_choice"),
        (
            {
                "messages": [USER],
                "tools": [{"type": "function", "function": {"name": "f", "strict": True}}],
            },
            "tools[0].function.strict",
        ),
        ({"messages": [USER], "tools": [{"type": "web_search"}]}, "tools[0]"),
        ({"messages": [{"role": "tool", "tool_call_id": "x", "content": "1"}]}, "messages[0]"),
        ({"messages": [{"role": "robot", "content": "1"}]}, "messages[0].role"),
        (
            {"messages": [{"role": "user", "content": [{"type": "input_audio"}]}]},
            "messages[0].content[0]",
        ),
    ],
)
def test_unsupported_shapes_are_refused_with_the_offending_path(
    body: dict[str, object], param: str
) -> None:
    with pytest.raises(Untranslatable) as refused:
        body_of(body)
    assert refused.value.refusal.param == param


def test_a_missing_output_ceiling_is_refused_so_the_hold_stays_bounded() -> None:
    # A body value is not a held ceiling: without the pipeline's, nothing is sent (M-15).
    req = request({"messages": [USER], "max_tokens": 10})
    bare = type(req)(req.endpoint, req.model, req.body)
    with pytest.raises(Untranslatable) as refused:
        provider().translate(bare, gemini_deployment())
    assert refused.value.refusal.param == "max_completion_tokens"


def test_only_chat_completions_are_served_embeddings_are_deferred() -> None:
    with pytest.raises(Untranslatable):
        provider().translate(request({}, endpoint="/v1/embeddings"), gemini_deployment())


# --- auth ----------------------------------------------------------------------------------------


def test_api_key_travels_in_a_header_and_never_in_the_url() -> None:
    for stream in (False, True):
        up = provider().translate(request({"messages": [USER]}, stream=stream), gemini_deployment())
        assert API_KEY not in up.url and "key=" not in up.url
        assert up.headers["x-goog-api-key"] == API_KEY
        assert "authorization" not in up.headers
        assert API_KEY not in up.body.decode()


def test_gemini_urls_are_config_base_plus_a_fixed_path_and_stream_uses_alt_sse() -> None:
    plain = provider().translate(request({"messages": [USER]}), gemini_deployment())
    streamed = provider().translate(request({"messages": [USER]}, stream=True), gemini_deployment())
    assert plain.url == "https://upstream.test/v1beta/models/gemini-4-carbon:generateContent"
    assert streamed.url.endswith("/models/gemini-4-carbon:streamGenerateContent?alt=sse")
    assert streamed.stream and not plain.stream and plain.method == "POST"


def test_vertex_uses_a_bearer_token_and_the_regional_project_path() -> None:
    creds = FakeCredentials()
    up = provider(creds).translate(request({"messages": [USER]}), vertex_deployment())
    assert up.url == (
        "https://upstream.test/v1/projects/demo-project/locations/europe-west4"
        "/publishers/google/models/gemini-4-carbon:generateContent"
    )
    assert up.headers["authorization"] == f"Bearer {TOKEN}"
    assert "x-goog-api-key" not in up.headers and TOKEN not in up.url


def test_vertex_refreshes_an_expired_token_once_and_reuses_a_valid_one() -> None:
    creds = FakeCredentials()
    adapter = provider(creds)
    req = request({"messages": [USER]})
    adapter.translate(req, vertex_deployment())
    adapter.translate(req, vertex_deployment())
    assert creds.refreshes == 1
    creds.expire()
    adapter.translate(req, vertex_deployment())
    assert creds.refreshes == 2


@pytest.mark.parametrize(
    "deployment",
    [
        vertex_deployment(region="../evil"),
        vertex_deployment(model="gemini/../x"),
        gemini_deployment(model="a b"),
        Deployment("google", "https://x.test", "m", AuthMode.API_KEY),
        Deployment("google", "https://x.test", "m", AuthMode.SIGV4),
    ],
)
def test_deployment_values_that_reach_the_url_or_auth_are_validated(deployment: Deployment) -> None:
    with pytest.raises(ValueError):
        provider().translate(request({"messages": [USER]}), deployment)


def test_vertex_needs_a_project_from_the_credentials() -> None:
    with pytest.raises(ValueError, match="project"):
        provider(FakeCredentials(project_id=None)).translate(
            request({"messages": [USER]}), vertex_deployment()
        )


def test_no_secret_appears_in_reprs_logs_or_the_body(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    adapter: GoogleProvider = provider()
    gemini, vertex = gemini_deployment(), vertex_deployment()
    up = adapter.translate(request({"messages": [USER]}), gemini)
    vertex_up = adapter.translate(request({"messages": [USER]}), vertex)
    shown = " ".join(map(repr, (adapter, gemini, vertex, up, vertex_up)))
    assert API_KEY not in shown and TOKEN not in shown
    assert API_KEY not in caplog.text and TOKEN not in caplog.text
    assert API_KEY not in up.body.decode() and TOKEN not in vertex_up.body.decode()


def test_default_credentials_are_built_lazily_from_service_account_json_without_network() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    info = {
        "type": "service_account",
        "project_id": "demo-project",
        "client_email": "sa@demo-project.iam.gserviceaccount.com",
        "private_key": pem,
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    deployment = Deployment(
        "google",
        "https://x.test",
        "m",
        AuthMode.BEARER_TOKEN,
        "europe-west4",
        credential=json.dumps(info),
    )
    creds = _service_account(deployment)
    assert creds.project_id == "demo-project" and not creds.valid  # a token is fetched on first use
    with pytest.raises(ValueError, match="service-account"):
        _service_account(replace(vertex_deployment(), credential=""))


def test_validate_refuses_other_endpoints_and_translate_raises_untranslatable() -> None:
    other = request({"input": "x"}, endpoint="/v1/embeddings")
    refusal = provider().validate(other)
    assert refusal is not None and refusal.param == "endpoint"
    assert provider().validate(request({"messages": [USER]})) is None
    with pytest.raises(Untranslatable) as err:
        provider().translate(other, gemini_deployment())
    assert err.value.refusal == refusal
