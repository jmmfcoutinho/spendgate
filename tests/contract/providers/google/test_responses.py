"""Gemini answers through the fake upstream: JSON, SSE, usage, thinking tokens, cost, errors."""

import json
from decimal import Decimal

import httpx
import pytest

from spendgate.core.cost import Usage, actual_cost
from spendgate.core.errors import ErrorCode
from spendgate.core.outcomes import Outcome
from spendgate.providers.google_stream import ChatStream
from spendgate.providers.port import (
    Deployment,
    Json,
    SseEvent,
    SseLimits,
    StreamBroken,
    StreamLimitExceeded,
    UpstreamRequest,
    UpstreamResponse,
)
from spendgate.providers.sse import serialise
from tests.contract.providers.google.helpers import (
    NOW_MS,
    USER,
    catalog,
    converting,
    dig,
    fixture,
    gemini_deployment,
    provider,
    request,
    vertex_deployment,
    wire,
)
from tests.fakes import FakeUpstream, FakeUsage, Script

pytestmark = pytest.mark.anyio
REQ = request({"messages": [USER]})

USAGE = FakeUsage(
    input_tokens=1000, output_tokens=300, cached_input_tokens=200, reasoning_tokens=700
)


async def send(fake: FakeUpstream, up: UpstreamRequest) -> httpx.Response:
    async with fake.client() as client:
        return await client.post(up.url, content=up.body, headers=dict(up.headers))


def as_response(raw: httpx.Response) -> UpstreamResponse:
    return UpstreamResponse(raw.status_code, dict(raw.headers), raw.content)


def chunks(events_: list[SseEvent]) -> list[Json]:
    return [json.loads(e.data) for e in events_ if e.data != "[DONE]"]


@pytest.mark.parametrize("deployment", [gemini_deployment(), vertex_deployment()])
async def test_non_stream_answer_becomes_an_openai_chat_completion(deployment: Deployment) -> None:
    fake = FakeUpstream(Script(usage=USAGE))
    up = provider().translate(request({"messages": [USER]}), deployment)
    out = provider().response(converting(), as_response(await send(fake, up)))
    body = json.loads(out.body)
    assert body["object"] == "chat.completion" and body["id"] == "chatcmpl-1"
    assert body["model"] == "gw-model" and body["created"] == 7
    assert body["choices"][0] == {
        "index": 0,
        "message": {"role": "assistant", "content": "Hello"},
        "finish_reason": "stop",
    }
    assert body["usage"]["prompt_tokens"] == 1000
    assert body["usage"]["completion_tokens"] == 1000  # candidates 300 + thoughts 700
    assert body["usage"]["prompt_tokens_details"] == {"cached_tokens": 200}
    assert body["usage"]["completion_tokens_details"] == {"reasoning_tokens": 700}
    assert fake.requests[0].path.endswith(":generateContent") and not fake.requests[0].query


def test_function_call_answer_becomes_tool_calls_and_keeps_the_thought_signature() -> None:
    raw = fixture("generate_content_function_call_20261117.json")
    out = provider().response(converting(), UpstreamResponse(200, {}, raw))
    choice = json.loads(out.body)["choices"][0]
    assert choice["finish_reason"] == "tool_calls" and choice["message"]["content"] is None
    call = choice["message"]["tool_calls"][0]
    assert call["id"] == "call_0" and json.loads(call["function"]["arguments"]) == {"city": "Porto"}
    assert call["extra_content"] == {"google": {"thought_signature": "sig-fixture"}}


@pytest.mark.parametrize(
    ("reason", "finish"),
    [("MAX_TOKENS", "length"), ("SAFETY", "content_filter"), ("OTHER", "stop")],
)
def test_gemini_finish_reasons_map_to_openai_finish_reasons(reason: str, finish: str) -> None:
    body = {"candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": reason}]}
    out = provider().response(converting(), UpstreamResponse(200, {}, json.dumps(body).encode()))
    assert dig(json.loads(out.body), "choices", 0, "finish_reason") == finish


def test_thought_summaries_are_not_part_of_the_answer_text() -> None:
    parts = [{"text": "private thinking", "thought": True}, {"text": "answer"}]
    body = {"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]}
    out = provider().response(converting(), UpstreamResponse(200, {}, json.dumps(body).encode()))
    assert dig(json.loads(out.body), "choices", 0, "message", "content") == "answer"


async def test_stream_alt_sse_becomes_openai_chunks_with_a_final_usage_chunk() -> None:
    fake = FakeUpstream(Script(usage=USAGE))
    body = {"messages": [USER], "stream": True, "stream_options": {"include_usage": True}}
    req = converting(body, stream=True)
    raw = await send(fake, provider().translate(req, gemini_deployment()))
    assert (
        fake.requests[0].query == {"alt": "sse"}
        and "text/event-stream" in raw.headers["content-type"]
    )
    stream = provider().relay(req)
    out = wire(stream.feed(raw.content) + stream.close())
    parsed = chunks(out)
    assert out[-1].data == "[DONE]"
    assert dig(parsed[0], "choices", 0, "delta", "role") == "assistant"
    text = "".join(str(dig(c, "choices", 0, "delta", "content") or "") for c in parsed)
    assert text == "Hello"
    assert dig(parsed[-1], "choices") == [] and dig(parsed[-1], "usage", "total_tokens") == 2000
    assert all(dig(c, "model") == "gw-model" and dig(c, "id") == "chatcmpl-1" for c in parsed)


def test_stream_tool_call_is_an_indexed_delta_with_a_tool_calls_finish() -> None:
    call = {"functionCall": {"name": "w", "args": {"q": "x"}}}
    chunk = {"candidates": [{"content": {"parts": [call]}, "finishReason": "STOP"}]}
    parsed = chunks(ChatStream("m", "i", 1).feed(SseEvent(None, json.dumps(chunk))))
    delta = dig(parsed[0], "choices", 0, "delta", "tool_calls", 0)
    assert dig(delta, "index") == 0 and dig(delta, "function", "name") == "w"
    assert dig(parsed[0], "choices", 0, "finish_reason") == "tool_calls"


# --- usage ---------------------------------------------------------------------------------------


async def test_usage_maps_prompt_cached_candidates_and_thoughts() -> None:
    fake = FakeUpstream(Script(usage=USAGE))
    up = provider().translate(request({"messages": [USER]}), gemini_deployment())
    usage = provider().usage(REQ, as_response(await send(fake, up)))
    assert usage == Usage(
        input_tokens=1000, output_tokens=1000, cached_input_tokens=200, reasoning_tokens=700
    )


def test_recorded_thinking_fixture_reads_the_same_usage() -> None:
    raw = fixture("generate_content_thinking_20261117.json")
    usage = provider().usage(REQ, UpstreamResponse(200, {}, raw))
    assert usage is not None and usage.reasoning_tokens == 700 and usage.output_tokens == 1000


def test_tool_use_prompt_tokens_count_as_input_so_usage_is_never_understated() -> None:
    meta = {"usageMetadata": {"promptTokenCount": 10, "toolUsePromptTokenCount": 5}}
    usage = provider().usage(REQ, UpstreamResponse(200, {}, json.dumps(meta).encode()))
    assert usage is not None and usage.input_tokens == 15


@pytest.mark.parametrize(
    "body", [b"{}", b"not json", b'{"usageMetadata": {"promptTokenCount": -1}}',
             b'{"usageMetadata": {"candidatesTokenCount": "7"}}'],
)  # fmt: skip
def test_absent_or_unreadable_usage_is_none_so_the_hold_is_charged(body: bytes) -> None:
    assert provider().usage(REQ, UpstreamResponse(200, {}, body)) is None


def _gemini(tokens: int | None, finish: str | None = None) -> bytes:
    """One Gemini SSE chunk: running totals of `tokens` prompt tokens, optionally finishing."""
    chunk: dict[str, Json] = {"candidates": [{"content": {"parts": [{"text": "x"}]}}]}
    if finish is not None:
        chunk["candidates"] = [{"content": {"parts": [{"text": "x"}]}, "finishReason": finish}]
    if tokens is not None:
        # The chunk has text, so its candidates count is required (a zero keeps the sums simple).
        chunk["usageMetadata"] = {"promptTokenCount": tokens, "candidatesTokenCount": 0}
    return serialise(SseEvent(None, json.dumps(chunk)))


async def test_stream_usage_is_the_totals_sent_with_the_finish_reason() -> None:
    fake = FakeUpstream(Script(usage=USAGE))
    up = provider().translate(request({"messages": [USER]}, stream=True), gemini_deployment())
    relay = provider().relay(converting(stream=True))
    relay.feed((await send(fake, up)).content)
    assert relay.usage() == Usage(1000, 1000, 200, reasoning_tokens=700)
    running = provider().relay(converting(stream=True))  # Gemini repeats totals; the last wins
    for raw in (_gemini(3), _gemini(9), _gemini(12, "STOP")):
        running.feed(raw)
    assert running.usage() == Usage(input_tokens=12)
    trailing = provider().relay(converting(stream=True))  # totals after the finish are final too
    trailing_totals = {"usageMetadata": {"promptTokenCount": 14, "candidatesTokenCount": 0}}
    totals_only = serialise(SseEvent(None, json.dumps(trailing_totals)))
    for raw in (_gemini(3, "STOP"), totals_only):
        trailing.feed(raw)
    assert trailing.usage() == Usage(input_tokens=14)


def test_m13_gemini_running_total_before_the_finish_reason_is_not_final_usage() -> None:
    asked = {"messages": [USER], "stream_options": {"include_usage": True}}
    cut = provider().relay(converting(asked, stream=True))
    for tokens in (3, 9):
        cut.feed(_gemini(tokens))
    assert cut.usage() is None  # a cut, cancel or stop now charges the full hold
    tail = cut.close()
    assert tail.endswith(b"data: [DONE]\n\n") and b'"usage"' not in tail  # no partial total


def test_m13_gemini_finish_without_its_own_usage_keeps_the_running_total_untrusted() -> None:
    relay = provider().relay(converting(stream=True))
    relay.feed(_gemini(9) + _gemini(None, "STOP"))
    assert relay.usage() is None


def test_m13_gemini_unspecified_finish_reason_does_not_make_usage_final() -> None:
    relay = provider().relay(converting(stream=True))
    relay.feed(_gemini(9, "FINISH_REASON_UNSPECIFIED"))
    assert relay.usage() is None


def test_m13_gemini_content_after_the_finish_makes_its_usage_untrusted_again() -> None:
    relay = provider().relay(converting(stream=True))
    relay.feed(_gemini(9, "STOP") + _gemini(11))  # odd order: more answer after the finish
    assert relay.usage() is None
    relay.feed(_gemini(15, "STOP"))  # a real finish again: those totals are final
    assert relay.usage() == Usage(input_tokens=15)


def test_m13_gemini_blocked_prompt_with_usage_is_final() -> None:
    body = {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": 4}}
    relay = provider().relay(converting(stream=True))
    relay.feed(serialise(SseEvent(None, json.dumps(body))))
    assert relay.usage() == Usage(input_tokens=4)


async def test_stream_without_usage_is_none_and_still_ends_with_done() -> None:
    fake = FakeUpstream(Script(usage=USAGE, final_usage=False))
    up = provider().translate(request({"messages": [USER]}, stream=True), gemini_deployment())
    stream = provider().relay(converting(stream=True))
    stream.feed((await send(fake, up)).content)
    assert stream.usage() is None
    assert stream.close() == b"data: [DONE]\n\n"


# --- cost ----------------------------------------------------------------------------------------


def test_thinking_tokens_are_priced_at_the_output_rate_as_the_catalog_says() -> None:
    entry = catalog().lookup("google", "gemini-4-carbon", NOW_MS)
    assert entry.meters.reasoning_per_mtok is None  # no reasoning meter: falls back to output
    usage = provider().usage(
        REQ, UpstreamResponse(200, {}, fixture("generate_content_thinking_20261117.json"))
    )
    assert usage is not None
    # 800 plain input at $2 + 200 cached at $0.10 + (300 answer + 700 thinking) output at $10
    expected = Decimal("800") * 2 + Decimal("200") * Decimal("0.1") + Decimal("1000") * 10
    assert actual_cost(entry, usage).to_decimal() == expected / 1_000_000


# --- errors --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "outcome"),
    [
        (429, ErrorCode.UPSTREAM_THROTTLED, Outcome.THROTTLED),
        (400, ErrorCode.INVALID_REQUEST, Outcome.CLIENT_ERROR),
        (500, ErrorCode.UPSTREAM_UNAVAILABLE, Outcome.UPSTREAM_5XX),
        (503, ErrorCode.UPSTREAM_UNAVAILABLE, Outcome.UPSTREAM_5XX),
    ],
)
async def test_fake_error_statuses_map_to_core_codes_and_outcomes(
    status: int, code: ErrorCode, outcome: Outcome
) -> None:
    fake = FakeUpstream(Script(status=status, retry_after="7"))
    up = provider().translate(request({"messages": [USER]}), gemini_deployment())
    err = provider().error(as_response(await send(fake, up)))
    free = catalog().free_statuses("google")
    assert err.code is code and err.status == status
    assert err.outcome(free) is outcome
    assert err.not_processed(free) is (status == 429)  # 429 may fail over for free
    assert err.retry_after_seconds == 7


def test_a_bad_api_key_or_missing_permission_is_our_fault_not_the_callers() -> None:
    key = {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "API key not valid",
                     "details": [{"reason": "API_KEY_INVALID"}]}}  # fmt: skip
    denied = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "no"}}
    for status, body in ((400, key), (403, denied)):
        err = provider().error(UpstreamResponse(status, {}, json.dumps(body).encode()))
        assert err.code is ErrorCode.UPSTREAM_UNAVAILABLE
    assert (
        provider().error(UpstreamResponse(502, {}, b"<html>")).code
        is ErrorCode.UPSTREAM_UNAVAILABLE
    )


def test_catalog_free_statuses_for_google_is_429_only() -> None:
    assert catalog().free_statuses("google") == frozenset({429})


def test_error_message_is_kept_out_of_reprs() -> None:
    body = json.dumps({"error": {"status": "INVALID_ARGUMENT", "message": "secret prompt text"}})
    err = provider().error(UpstreamResponse(400, {}, body.encode()))
    assert err.message == "secret prompt text" and "secret prompt text" not in repr(err)


def test_a_blocked_prompt_maps_to_the_content_filter_finish_reason() -> None:
    body = json.dumps({"promptFeedback": {"blockReason": "SAFETY"}}).encode()
    out = provider().response(converting(), UpstreamResponse(200, {}, body))
    assert dig(json.loads(out.body), "choices", 0, "finish_reason") == "content_filter"
    streamed = provider().relay(converting(stream=True))
    sent_chunks = chunks(wire(streamed.feed(serialise(SseEvent(None, body.decode())))))
    assert dig(sent_chunks[0], "choices", 0, "finish_reason") == "content_filter"


def test_the_usage_only_chunk_reaches_the_client_only_when_it_asked() -> None:
    meta = {"usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4}}
    raw = serialise(SseEvent(None, json.dumps(meta | {"candidates": [{"finishReason": "STOP"}]})))
    asked = {"messages": [USER], "stream_options": {"include_usage": True}}
    plain, shown = (
        provider().relay(converting(stream=True)),
        provider().relay(converting(asked, stream=True)),
    )
    quiet = wire(plain.feed(raw) + plain.close())
    loud = wire(shown.feed(raw) + shown.close())
    assert not any(dig(c, "usage") for c in chunks(quiet)) and quiet[-1].data == "[DONE]"
    assert dig(chunks(loud)[-1], "usage", "prompt_tokens") == 3
    assert plain.usage() == shown.usage() is not None


def test_relay_enforces_limits_and_rejects_a_half_event() -> None:
    req = converting(stream=True)
    with pytest.raises(StreamLimitExceeded):
        provider().relay(req, SseLimits(max_stream_bytes=8)).feed(b"data: 123456789\n\n")
    with pytest.raises(StreamLimitExceeded):
        provider().relay(req, SseLimits(max_events=1)).feed(b"data: {}\n\ndata: {}\n\n")
    half = provider().relay(req)
    half.feed(b'data: {"candidates"')
    with pytest.raises(StreamBroken):
        half.close()


def test_an_unreadable_answer_becomes_a_bodiless_502() -> None:
    out = provider().response(converting(), UpstreamResponse(200, {}, b"<html>"))
    assert (out.status, out.body) == (502, b"")
