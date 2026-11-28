"""Response side: usage per endpoint, costs against the catalog, errors, the fake, secrecy."""

import json
import logging
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import yaml

from spendgate.core.catalog import Catalog, parse
from spendgate.core.cost import Usage, actual_cost
from spendgate.core.errors import ErrorCode
from spendgate.core.outcomes import Outcome
from spendgate.providers import openai_shapes as shapes
from spendgate.providers.openai import OpenAIProvider
from spendgate.providers.port import (
    CleanRequest,
    Deployment,
    Json,
    SseEvent,
    SseLimits,
    StreamBroken,
    StreamLimitExceeded,
    Untranslatable,
    UpstreamResponse,
)
from spendgate.providers.sse import serialise
from tests.contract.providers.openai.conftest import SECRET, answer, fixture
from tests.fakes import BASE_URL, FakeUpstream, FakeUsage, Script
from tests.unit.audio_builders import wav

pytestmark = pytest.mark.anyio
REQ = CleanRequest(shapes.CHAT, "m", {})
EMBED = CleanRequest(shapes.EMBEDDINGS, "m", {})  # the one endpoint with no output count
RESPONSES = CleanRequest(shapes.RESPONSES, "m", {})
TRANSCRIBE = CleanRequest(shapes.TRANSCRIPTIONS, "m", {})
PRICES = Path(__file__).parents[4] / "src/spendgate/catalog/prices.yaml"


def events(name: str) -> list[SseEvent]:
    data = fixture(name)
    assert isinstance(data, list)
    return [SseEvent(None, json.dumps(item)) for item in data]


def collect(
    items: list[SseEvent], provider: OpenAIProvider, endpoint: str = shapes.CHAT
) -> Usage | None:
    relay = provider.relay(CleanRequest(endpoint, "m", {}, stream=True))
    for event in items:
        relay.feed(serialise(event))
    return relay.usage()


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    doc = yaml.safe_load(PRICES.read_text())
    return parse([doc], "0" * 64, ledger_currency="USD")


def cost(catalog: Catalog, model: str, usage: Usage) -> Decimal:
    entry = catalog.lookup("openai", model, int(time.time() * 1000))
    return actual_cost(entry, usage).to_decimal()


CHAT_USAGE = Usage(
    input_tokens=1200, output_tokens=500, cached_input_tokens=200, reasoning_tokens=100
)


def test_chat_usage_maps_input_output_cached_and_reasoning(provider: OpenAIProvider) -> None:
    assert provider.usage(REQ, answer("chat_completion.json")) == CHAT_USAGE


def test_chat_audio_tokens_move_to_the_audio_counters(provider: OpenAIProvider) -> None:
    usage = provider.usage(REQ, answer("chat_completion_audio.json"))
    assert usage == Usage(
        input_tokens=200, output_tokens=50, audio_input_tokens=100, audio_output_tokens=30
    )


def test_responses_embeddings_and_transcription_usage(provider: OpenAIProvider) -> None:
    assert provider.usage(RESPONSES, answer("responses.json")) == CHAT_USAGE
    assert provider.usage(EMBED, answer("embeddings.json")) == Usage(input_tokens=8)
    assert provider.usage(TRANSCRIBE, answer("transcription_tokens.json")) == Usage(
        input_tokens=14, output_tokens=20, audio_input_tokens=200
    )
    assert provider.usage(TRANSCRIBE, answer("transcription_duration.json")) == Usage(
        audio_seconds=Decimal("3.5")
    )


def test_speech_usage_comes_from_the_request_and_is_billed_by_the_inputs_characters(
    provider: OpenAIProvider,
) -> None:
    audio = UpstreamResponse(200, {"Content-Type": "audio/mpeg"}, b"ID3\x00\xff\xfb\x90\x00")
    speech = CleanRequest(shapes.SPEECH, "m", {"input": "héllo", "voice": "alloy"})
    assert provider.usage(speech, audio) == Usage(characters=5)
    assert provider.usage(speech, UpstreamResponse(500, {}, b"")) is None
    assert provider.usage(REQ, audio) is None  # any other endpoint reads the body


UNREADABLE = [
    b"", b"not json", b"[]", b'{"usage": null}', b'{"usage": {"prompt_tokens": -1}}',
    b'{"usage": {"prompt_tokens": 1.5}}', b'{"usage": {"prompt_tokens": true}}',
    b'{"usage": {"type": "duration", "seconds": "3"}}',
    b'{"usage": {"type": "duration", "seconds": -1}}', b'{"usage": {"completion_tokens": 3}}',
]  # fmt: skip


@pytest.mark.parametrize("body", UNREADABLE)
def test_unreadable_usage_is_none_so_the_full_hold_is_charged(
    provider: OpenAIProvider, body: bytes
) -> None:
    assert provider.usage(REQ, UpstreamResponse(200, {}, body)) is None


def test_costs_match_the_catalog_for_recorded_usage(
    provider: OpenAIProvider, catalog: Catalog
) -> None:
    usage = provider.usage(REQ, answer("chat_completion.json"))
    assert usage is not None
    # gpt-6-luna per Mtok: input 0.1, cached 0.01, output 0.5 (reasoning is output-priced).
    expected = (Decimal(1000) * Decimal("0.1") + Decimal(200) * Decimal("0.01")
                + Decimal(500) * Decimal("0.5")) / 1_000_000  # fmt: skip
    assert cost(catalog, "gpt-6-luna", usage) == expected
    emb = provider.usage(EMBED, answer("embeddings.json"))
    assert emb is not None
    assert cost(catalog, "text-embedding-3-small", emb) == Decimal(8) * Decimal("0.02") / 1_000_000


def test_a_stream_without_final_usage_gives_none_so_the_pipeline_charges_the_hold(
    provider: OpenAIProvider,
) -> None:
    without_usage = [*events("chat_stream_events.json")[:2], SseEvent(None, "[DONE]")]
    assert collect(without_usage, provider) is None
    cut = events("responses_stream_events.json")[:2]
    assert collect(cut, provider) is None


def test_stream_usage_is_read_from_chat_chunks_and_the_responses_terminal_event(
    provider: OpenAIProvider,
) -> None:
    chat = [*events("chat_stream_events.json"), SseEvent(None, "[DONE]")]
    assert collect(chat, provider) == CHAT_USAGE
    named = [SseEvent("response.completed", e.data) for e in events("responses_stream_events.json")]
    assert collect(named, provider, shapes.RESPONSES) == CHAT_USAGE
    assert collect([SseEvent(None, "{broken")], provider) is None


type ErrorCase = tuple[str, int, dict[str, str], ErrorCode, Outcome]
THROTTLED, UNAVAILABLE = ErrorCode.UPSTREAM_THROTTLED, ErrorCode.UPSTREAM_UNAVAILABLE
ERROR_TABLE: list[ErrorCase] = [
    ("error_429.json", 429, {"Retry-After": "7"}, THROTTLED, Outcome.THROTTLED),
    ("error_429.json", 429, {"retry-after": "1.2"}, THROTTLED, Outcome.THROTTLED),
    ("error_429_quota.json", 429, {}, UNAVAILABLE, Outcome.THROTTLED),
    ("error_context_length.json", 400, {}, ErrorCode.REQUEST_TOO_LARGE, Outcome.CLIENT_ERROR),
    ("error_context_length.json", 413, {}, ErrorCode.REQUEST_TOO_LARGE, Outcome.CLIENT_ERROR),
    ("error_401.json", 401, {}, UNAVAILABLE, Outcome.CLIENT_ERROR),
    ("error_500.json", 500, {}, UNAVAILABLE, Outcome.UPSTREAM_5XX),
    ("error_500.json", 503, {}, UNAVAILABLE, Outcome.UPSTREAM_5XX),
    ("error_500.json", 408, {}, UNAVAILABLE, Outcome.UPSTREAM_5XX),
    ("error_401.json", 422, {}, ErrorCode.INVALID_REQUEST, Outcome.CLIENT_ERROR),
]


@pytest.mark.parametrize("case", ERROR_TABLE)
def test_error_mapping_table(provider: OpenAIProvider, case: ErrorCase) -> None:
    name, status, headers, code, outcome = case
    error = provider.error(answer(name, status, headers))
    assert (error.code, error.status, error.sent) == (code, status, True)
    assert error.outcome({429}) is outcome
    # Only a free status is failover-safe; the default catalog rule for openai is [429].
    assert error.not_processed({429}) is (status == 429)


def openai_error_body(code: str | None) -> bytes:
    error = {"message": "provider text", "type": "invalid_request_error", "code": code}
    return json.dumps({"error": error}).encode()


# (status, error.code, deployment fault): only a code that fails for every caller counts towards
# the shared breaker (ADR-0033); the pipeline counts any 401 by status on its own.
DEPLOYMENT_FAULTS = [
    (401, "invalid_api_key", True),
    (404, "model_not_found", True),
    (403, "unsupported_country_region_territory", True),
    (403, None, False),  # "You are not allowed to generate embeddings from this model" (T-012)
    (404, None, False),  # a model that is not served on this endpoint
    (400, "invalid_value", False),
    (429, "insufficient_quota", False),  # a quota 429 cools the deployment; it is not a fault
]


@pytest.mark.parametrize(("status", "code", "fault"), DEPLOYMENT_FAULTS)
def test_only_a_bad_key_or_missing_model_code_is_a_deployment_fault(
    provider: OpenAIProvider, status: int, code: str | None, fault: bool
) -> None:
    error = provider.error(UpstreamResponse(status, {}, openai_error_body(code)))
    assert error.deployment_fault is fault
    assert provider.error(answer("error_401.json", 401)).deployment_fault  # the recorded fixture


def test_throttle_carries_retry_after_and_junk_does_not(provider: OpenAIProvider) -> None:
    assert (
        provider.error(answer("error_429.json", 429, {"Retry-After": "7"})).retry_after_seconds == 7
    )
    assert (
        provider.error(answer("error_429.json", 429, {"retry-after": "1.2"})).retry_after_seconds
        == 2
    )
    date = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
    assert provider.error(answer("error_429.json", 429, date)).retry_after_seconds is None
    assert provider.error(UpstreamResponse(502, {}, b"<html>bad gateway</html>")).code is (
        ErrorCode.UPSTREAM_UNAVAILABLE
    )


def test_the_provider_message_is_kept_but_hidden_from_reprs(provider: OpenAIProvider) -> None:
    error = provider.error(answer("error_401.json", 401))
    assert "Incorrect API key" in error.message
    assert "Incorrect API key" not in repr(error)


# --- against the fake upstream -----------------------------------------------------------------

type Case = tuple[str, dict[str, Json], dict[str, bytes], bool]
REQUESTS: list[Case] = [
    (shapes.CHAT, {"messages": [{"role": "user", "content": "hi"}]}, {}, False),
    (shapes.CHAT, {"messages": [{"role": "user", "content": "hi"}], "stream": True}, {}, True),
    (shapes.RESPONSES, {"input": "hi"}, {}, False),
    (shapes.RESPONSES, {"input": "hi", "stream": True}, {}, True),
    (shapes.EMBEDDINGS, {"input": "hi"}, {}, False),
    (shapes.TRANSCRIPTIONS, {}, {"file": wav(1.0)}, False),
]


async def run(
    fake: FakeUpstream, provider: OpenAIProvider, deployment: Deployment, request: CleanRequest
) -> tuple[httpx.Response, list[SseEvent]]:
    sent = provider.translate(request, deployment)
    async with fake.client() as client:
        response = await client.request(
            sent.method, sent.url, headers=dict(sent.headers), content=sent.body
        )
    parsed: list[SseEvent] = []
    for frame in response.text.split("\n\n"):
        data = [line[6:] for line in frame.splitlines() if line.startswith("data: ")]
        if data:
            parsed.append(SseEvent(None, "\n".join(data)))
    return response, parsed


@pytest.mark.parametrize("case", REQUESTS)
async def test_every_endpoint_works_against_the_fake_and_usage_is_read(
    provider: OpenAIProvider, deployment: Deployment, case: Case
) -> None:
    endpoint, body, files, streams = case
    fake = FakeUpstream(
        Script(usage=FakeUsage(input_tokens=11, output_tokens=7, audio_seconds=Decimal(2)))
    )
    request = CleanRequest(endpoint, "m", body, stream=streams, files=files, output_ceiling=64)
    response, parsed = await run(fake, provider, deployment, request)
    assert response.status_code == 200
    assert fake.requests[0].headers["authorization"] == f"Bearer {SECRET}"
    if streams:
        assert collect(parsed, provider, endpoint) == Usage(input_tokens=11, output_tokens=7)
        return
    usage = provider.usage(request, UpstreamResponse(200, {}, response.content))
    if endpoint == shapes.TRANSCRIPTIONS:
        assert usage == Usage(audio_seconds=Decimal(2))
    elif endpoint == shapes.EMBEDDINGS:
        assert usage == Usage(input_tokens=11)
    else:
        assert usage == Usage(input_tokens=11, output_tokens=7)


async def test_speech_against_the_fake_returns_audio_billed_by_characters(
    provider: OpenAIProvider, deployment: Deployment
) -> None:
    fake = FakeUpstream()
    req = CleanRequest(shapes.SPEECH, "m", {"input": "hello", "voice": "alloy"})
    response, _ = await run(fake, provider, deployment, req)
    assert response.headers["content-type"] == "audio/mpeg"
    audio = UpstreamResponse(200, {}, response.content)
    assert provider.usage(req, audio) == Usage(characters=5)
    assert fake.requests[0].json()["model"] == "gpt-upstream"


async def test_chat_stream_through_the_fake_hides_an_injected_usage_chunk(
    provider: OpenAIProvider, deployment: Deployment
) -> None:
    fake = FakeUpstream()
    body: dict[str, Json] = {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    request = CleanRequest(shapes.CHAT, "m", body, stream=True, output_ceiling=64)
    _, parsed = await run(fake, provider, deployment, request)
    relay = provider.relay(request)
    visible = [e for p in parsed for e in parse_wire(relay.feed(serialise(p)))]
    assert len(visible) == len(parsed) - 1
    assert not any(shapes_chunk_has_usage(e) for e in visible if e.data != "[DONE]")


def parse_wire(raw: bytes) -> list[SseEvent]:
    return [SseEvent(None, f[6:]) for f in raw.decode().split("\n\n") if f.startswith("data: ")]


def shapes_chunk_has_usage(event: SseEvent) -> bool:
    return bool(json.loads(event.data).get("usage"))


async def test_a_fake_stream_with_no_final_usage_yields_none(
    provider: OpenAIProvider, deployment: Deployment
) -> None:
    fake = FakeUpstream(Script(final_usage=False))
    body: dict[str, Json] = {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    request = CleanRequest(shapes.CHAT, "m", body, stream=True, output_ceiling=64)
    _, parsed = await run(fake, provider, deployment, request)
    assert collect(parsed, provider) is None


async def test_the_fakes_429_maps_to_a_free_throttle_with_retry_after(
    provider: OpenAIProvider, deployment: Deployment
) -> None:
    fake = FakeUpstream(Script(status=429, retry_after="3"))
    req = CleanRequest(
        shapes.CHAT, "m", {"messages": [{"role": "user", "content": "x"}]}, output_ceiling=64
    )
    response, _ = await run(fake, provider, deployment, req)
    error = provider.error(
        UpstreamResponse(response.status_code, dict(response.headers), response.content)
    )
    assert (error.code, error.retry_after_seconds) == (ErrorCode.UPSTREAM_THROTTLED, 3)
    assert error.not_processed({429}) and BASE_URL


def test_no_secret_or_user_content_in_reprs_or_logs(
    provider: OpenAIProvider, deployment: Deployment, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    req = CleanRequest(
        shapes.CHAT,
        "m",
        {"messages": [{"role": "user", "content": "private words"}]},
        output_ceiling=64,
    )
    sent = provider.translate(req, deployment)
    refusal = provider.validate(
        CleanRequest(shapes.CHAT, "m", {"messages": [], "private_field": "private words"})
    )
    text = repr(deployment) + repr(sent) + repr(refusal) + caplog.text
    assert SECRET not in text
    assert "private words" not in text
    with pytest.raises(Untranslatable) as caught:
        provider.translate(
            CleanRequest(
                shapes.CHAT,
                "m",
                {"messages": [{"role": "user", "content": "private words"}], "z": "private words"},
            ),
            deployment,
        )
    assert "private words" not in str(caught.value) + repr(caught.value.refusal)


def test_response_is_the_upstream_answer_unchanged(provider: OpenAIProvider) -> None:
    resp = answer("chat_completion.json")
    assert provider.response(CleanRequest(shapes.CHAT, "m", {}), resp) is resp


def test_relay_enforces_limits_and_rejects_a_half_event(provider: OpenAIProvider) -> None:
    req = CleanRequest(shapes.CHAT, "m", {}, stream=True)
    with pytest.raises(StreamLimitExceeded):
        provider.relay(req, SseLimits(max_stream_bytes=10)).feed(b"data: " + b"x" * 20 + b"\n\n")
    half = provider.relay(req)
    half.feed(b'data: {"choices": [')
    with pytest.raises(StreamBroken):
        half.close()


def test_remote_image_urls_pass_validate_and_translate_only_when_configured(
    deployment: Deployment,
) -> None:
    part: Json = {"type": "image_url", "image_url": {"url": "https://img.test/x.png"}}
    body: dict[str, Json] = {"messages": [{"role": "user", "content": [part]}]}
    req = CleanRequest(shapes.CHAT, "m", body, output_ceiling=64)
    assert OpenAIProvider().validate(req) is not None
    with pytest.raises(Untranslatable):
        OpenAIProvider().translate(req, deployment)
    permissive = OpenAIProvider(remote_ok=True)
    assert permissive.validate(req) is None
    assert permissive.translate(req, deployment).body


def test_m13_only_end_of_stream_usage_is_final(provider: OpenAIProvider) -> None:
    """Running totals on content chunks or `response.in_progress` are partial: never charged."""
    running = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    partial = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    content = {"choices": [{"index": 0, "delta": {"content": "x"}}], "usage": running}
    progress = {"type": "response.in_progress", "response": {"usage": partial}}
    assert collect([SseEvent(None, json.dumps(content))], provider) is None
    in_progress = [SseEvent("response.in_progress", json.dumps(progress))]
    assert collect(in_progress, provider, shapes.RESPONSES) is None
    for kind in ("response.completed", "response.incomplete", "response.failed"):
        final = {"type": kind, "response": {"usage": partial}}
        assert collect([SseEvent(kind, json.dumps(final))], provider, shapes.RESPONSES) == Usage(
            1, 1
        )
    usage_only = {"choices": [], "usage": running}
    assert collect([SseEvent(None, json.dumps(usage_only))], provider) == Usage(1, 1)


def test_m13_content_after_the_final_usage_makes_it_untrusted(provider: OpenAIProvider) -> None:
    """Odd order (more answer after the usage-only chunk or a terminal event) is not trusted."""
    running = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    usage_only = SseEvent(None, json.dumps({"choices": [], "usage": running}))
    content = SseEvent(None, json.dumps({"choices": [{"index": 0, "delta": {"content": "x"}}]}))
    assert collect([usage_only, content], provider) is None
    assert collect([usage_only, SseEvent(None, "[DONE]")], provider) == Usage(1, 1)
    usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    done = SseEvent(None, json.dumps({"type": "response.completed", "response": {"usage": usage}}))
    delta = SseEvent(None, json.dumps({"type": "response.output_text.delta", "delta": "x"}))
    assert collect([done], provider, shapes.RESPONSES) == Usage(1, 1)
    assert collect([done, delta], provider, shapes.RESPONSES) is None
