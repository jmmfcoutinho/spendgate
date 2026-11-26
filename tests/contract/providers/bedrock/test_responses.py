"""Converse answers through the fake upstream: JSON, event-stream, usage, cost and errors."""

import json
import struct
import zlib
from decimal import Decimal
from importlib import resources

import httpx
import pytest
import yaml

from spendgate.core.catalog import Catalog, parse
from spendgate.core.cost import actual_cost
from spendgate.core.errors import ErrorCode
from spendgate.core.outcomes import Outcome
from spendgate.providers.bedrock_stream import (
    BedrockStream,
    EventStreamDecoder,
    EventStreamError,
)
from spendgate.providers.port import (
    Json,
    SseEvent,
    SseLimits,
    StreamBroken,
    StreamLimitExceeded,
    UpstreamRequest,
    UpstreamResponse,
)
from tests.contract.providers.bedrock.helpers import (
    adapter,
    converting,
    deployment,
    dig,
    request,
    wire,
)
from tests.fakes import FakeUpstream, FakeUsage, Script
from tests.fakes.frames import eventstream_frame

pytestmark = pytest.mark.anyio
USER = {"role": "user", "content": "hi"}
REQ = request({"messages": [USER]})
USAGE = FakeUsage(
    input_tokens=1000, output_tokens=500, cached_input_tokens=200, cache_write_tokens=100
)
NOW_MS = 1_794_000_000_000  # 2026-11-06: after the bundled Bedrock entries take effect


async def send(fake: FakeUpstream, up: UpstreamRequest) -> httpx.Response:
    async with fake.client() as client:
        return await client.post(up.url, content=up.body, headers=dict(up.headers))


def catalog() -> Catalog:
    text = (resources.files("spendgate.catalog") / "prices.yaml").read_text(encoding="utf-8")
    return parse([yaml.safe_load(text)], "0" * 64, ledger_currency="USD")


def chunks(events: list[SseEvent]) -> list[Json]:
    return [json.loads(e.data) for e in events if e.data != "[DONE]"]


async def test_non_stream_answer_becomes_an_openai_chat_completion() -> None:
    fake = FakeUpstream(Script(usage=USAGE))
    up = adapter().translate(request({"messages": [USER]}), deployment())
    raw = await send(fake, up)
    out = adapter().response(
        converting({"messages": [USER]}),
        UpstreamResponse(raw.status_code, dict(raw.headers), raw.content),
    )
    body = json.loads(out.body)
    assert body["object"] == "chat.completion" and body["model"] == "gw-model"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "Hello"}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 1300 and body["usage"]["completion_tokens"] == 500
    assert body["usage"]["prompt_tokens_details"] == {"cached_tokens": 200}
    assert fake.requests[0].path.endswith("/converse")


async def test_converse_tool_use_becomes_tool_calls_with_a_tool_calls_finish() -> None:
    converse = {
        "output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "t1", "name": "weather", "input": {"q": "x"}}},
        ]}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 3, "outputTokens": 4, "totalTokens": 7},
    }  # fmt: skip
    out = adapter().response(
        converting({"messages": [USER]}), UpstreamResponse(200, {}, json.dumps(converse).encode())
    )
    choice = json.loads(out.body)["choices"][0]
    assert choice["finish_reason"] == "tool_calls" and choice["message"]["content"] is None
    call = choice["message"]["tool_calls"][0]
    assert call["id"] == "t1" and json.loads(call["function"]["arguments"]) == {"q": "x"}


async def test_stream_decodes_event_stream_into_openai_chunks_and_reads_usage() -> None:
    fake = FakeUpstream(Script(usage=USAGE))
    body = {"messages": [USER], "stream": True, "stream_options": {"include_usage": True}}
    req = converting(body, stream=True)
    raw = await send(fake, adapter().translate(req, deployment()))
    assert raw.headers["content-type"] == "application/vnd.amazon.eventstream"
    stream = adapter().relay(req)
    out = b""
    for i in range(0, len(raw.content), 7):  # arbitrary network splits
        out += stream.feed(raw.content[i : i + 7])
    out += stream.close()
    events = wire(out)
    parsed = chunks(events)
    assert dig(parsed[0], "model") == "gw-model" and dig(parsed[0], "id") == "chatcmpl-1"
    assert events[-1].data == "[DONE]"
    text = "".join(str(dig(c, "choices", 0, "delta", "content") or "") for c in parsed)
    assert text == "Hello"
    assert dig(parsed[-1], "choices") == [] and dig(parsed[-1], "usage", "prompt_tokens") == 1300
    assert dig(parsed[-2], "choices", 0, "finish_reason") == "stop"
    usage = stream.usage()
    assert usage is not None and usage.input_tokens == 1300


async def test_stream_without_metadata_has_no_usage_and_still_ends_with_done() -> None:
    fake = FakeUpstream(Script(usage=USAGE, final_usage=False))
    up = adapter().translate(request({"messages": [USER]}, stream=True), deployment())
    raw = await send(fake, up)
    stream = BedrockStream("m", "i", 1)
    events = wire(stream.feed(raw.content) + stream.close())
    assert stream.usage() is None and events[-1].data == "[DONE]"


def test_stream_tool_call_deltas_are_indexed_openai_tool_calls() -> None:
    frames = b"".join([
        eventstream_frame("messageStart", {"role": "assistant"}),
        eventstream_frame("contentBlockStart", {
            "contentBlockIndex": 1, "start": {"toolUse": {"toolUseId": "t1", "name": "w"}}}),
        eventstream_frame("contentBlockDelta", {
            "contentBlockIndex": 1, "delta": {"toolUse": {"input": '{"q":'}}}),
        eventstream_frame("contentBlockDelta", {
            "contentBlockIndex": 1, "delta": {"toolUse": {"input": '"x"}'}}}),
        eventstream_frame("messageStop", {"stopReason": "tool_use"}),
    ])  # fmt: skip
    stream = BedrockStream("m", "i", 1)
    parsed = chunks(wire(stream.feed(frames)))
    calls = [dig(c, "choices", 0, "delta", "tool_calls", 0) for c in parsed]
    calls = [c for c in calls if c is not None]
    assert dig(calls[0], "index") == 0 and dig(calls[0], "id") == "t1"
    args = "".join(str(dig(c, "function", "arguments") or "") for c in calls)
    assert args == '{"q":"x"}'
    assert dig(parsed[-1], "choices", 0, "finish_reason") == "tool_calls"


def test_stream_exception_frame_becomes_an_error_chunk_then_done() -> None:
    body = json.dumps({"message": "slow down: secret details"}).encode()
    headers = {":message-type": "exception", ":exception-type": "throttlingException"}
    frame = _frame(headers, body)
    events = wire(BedrockStream("m", "i", 1).feed(frame))
    assert events[-1].data == "[DONE]"
    error = json.loads(events[0].data)["error"]
    assert error["code"] == "upstream_throttled" and "secret" not in events[0].data


# --- framing guards (T-22: verify CRCs, reject oversize frames) ----------------------------


def _frame(headers: dict[str, str], payload: bytes) -> bytes:
    raw = b""
    for name, value in headers.items():
        n, v = name.encode(), value.encode()
        raw += bytes([len(n)]) + n + b"\x07" + struct.pack(">H", len(v)) + v
    total = 16 + len(raw) + len(payload)
    prelude = struct.pack(">II", total, len(raw))
    head = prelude + struct.pack(">I", zlib.crc32(prelude))
    message = head + raw + payload
    return message + struct.pack(">I", zlib.crc32(message))


def test_a_corrupt_prelude_crc_is_rejected() -> None:
    frame = bytearray(eventstream_frame("messageStart", {"role": "assistant"}))
    frame[9] ^= 0xFF
    with pytest.raises(EventStreamError, match="prelude CRC"):
        EventStreamDecoder().feed(bytes(frame))


def test_a_corrupt_payload_or_message_crc_is_rejected() -> None:
    frame = bytearray(eventstream_frame("messageStart", {"role": "assistant"}))
    frame[-6] ^= 0x01
    with pytest.raises(EventStreamError, match="message CRC"):
        EventStreamDecoder().feed(bytes(frame))


def test_an_oversize_frame_is_rejected_from_its_prelude_before_it_is_buffered() -> None:
    prelude = struct.pack(">II", 10_000_000, 0)
    head = prelude + struct.pack(">I", zlib.crc32(prelude))
    with pytest.raises(EventStreamError, match="out of bounds"):
        EventStreamDecoder(max_frame_bytes=1024 * 1024).feed(head)


def test_a_frame_shorter_than_its_declared_headers_is_rejected() -> None:
    prelude = struct.pack(">II", 20, 50)
    with pytest.raises(EventStreamError, match="out of bounds"):
        EventStreamDecoder().feed(prelude + struct.pack(">I", zlib.crc32(prelude)))


def test_a_stream_cut_inside_a_frame_is_an_error_on_close() -> None:
    frame = eventstream_frame("messageStart", {"role": "assistant"})
    decoder = EventStreamDecoder()
    assert decoder.feed(frame[:-3]) == []
    with pytest.raises(EventStreamError, match="inside a frame"):
        decoder.close()


# --- usage and cost --------------------------------------------------------------------------


def bedrock_catalog_ids() -> list[str]:
    """Every bundled Bedrock model id: each geographic, global and in-Region profile (#100)."""
    text = (resources.files("spendgate.catalog") / "prices.yaml").read_text(encoding="utf-8")
    ids = [e["id"] for e in yaml.safe_load(text)["models"] if e["id"].startswith("bedrock/")]
    return sorted(i.removeprefix("bedrock/") for i in ids)


@pytest.mark.parametrize("model", bedrock_catalog_ids())
async def test_usage_prices_through_the_bundled_bedrock_catalog_entries(model: str) -> None:
    fake = FakeUpstream(Script(usage=USAGE))
    up = adapter().translate(request({"messages": [USER]}), deployment(model))
    raw = await send(fake, up)
    usage = adapter().usage(REQ, UpstreamResponse(raw.status_code, {}, raw.content))
    assert usage is not None
    assert (usage.input_tokens, usage.cached_input_tokens, usage.cache_write_tokens) == (
        1300,
        200,
        100,
    )
    entry = catalog().lookup("bedrock", model, NOW_MS)
    cost = actual_cost(entry, usage)
    m = entry.meters
    expected = (
        Decimal(1000) * m.input_per_mtok + Decimal(200) * m.cached_input_per_mtok  # type: ignore[operator]
        + Decimal(100) * m.cache_write_per_mtok + Decimal(500) * m.output_per_mtok  # type: ignore[operator]
    ) / 1_000_000  # fmt: skip
    assert cost.nano == int(expected * 10**9)  # exact for these prices: no rounding involved


def test_usage_through_the_relay_equals_non_stream_usage() -> None:
    meta = {"usage": {"inputTokens": 5, "outputTokens": 6, "totalTokens": 11,
                      "cacheReadInputTokens": 2, "cacheWriteInputTokens": 1}}  # fmt: skip
    stream = adapter().relay(request({"messages": [USER]}, stream=True))
    stream.feed(eventstream_frame("metadata", meta))
    result = stream.usage()
    non_stream = json.dumps({"output": {"message": {"content": []}}, **meta}).encode()
    assert result == adapter().usage(REQ, UpstreamResponse(200, {}, non_stream))
    assert result is not None and result.input_tokens == 8


BAD_USAGE: list[Json] = [
    None,
    {},
    {"inputTokens": 1},
    {"inputTokens": -1, "outputTokens": 1},
    {"inputTokens": True, "outputTokens": 1},
    {"inputTokens": 1, "outputTokens": 1, "cacheReadInputTokens": "x"},
]


@pytest.mark.parametrize("usage", BAD_USAGE)
def test_absent_or_unreadable_usage_is_none_so_the_hold_is_charged(usage: Json) -> None:
    body: dict[str, Json] = {
        "output": {"message": {"role": "assistant", "content": [{"text": "x"}]}}
    }
    if usage is not None:
        body["usage"] = usage
    assert adapter().usage(REQ, UpstreamResponse(200, {}, json.dumps(body).encode())) is None
    assert adapter().usage(REQ, UpstreamResponse(200, {}, b"not json")) is None


# --- errors ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "kind", "code", "outcome", "free"),
    [
        (429, "ThrottlingException", ErrorCode.UPSTREAM_THROTTLED, Outcome.THROTTLED, True),
        (429, "ModelNotReadyException", ErrorCode.UPSTREAM_THROTTLED, Outcome.THROTTLED, True),
        (
            503,
            "ServiceUnavailableException",
            ErrorCode.UPSTREAM_UNAVAILABLE,
            Outcome.UPSTREAM_5XX,
            False,
        ),
        (
            500,
            "InternalServerException",
            ErrorCode.UPSTREAM_UNAVAILABLE,
            Outcome.UPSTREAM_5XX,
            False,
        ),
        (424, "ModelErrorException", ErrorCode.UPSTREAM_UNAVAILABLE, Outcome.CLIENT_ERROR, False),
        (400, "ValidationException", ErrorCode.INVALID_REQUEST, Outcome.CLIENT_ERROR, False),
        # Our credentials or model config, not the caller's: upstream_unavailable (protocol 4.2 r1).
        (403, "AccessDeniedException", ErrorCode.UPSTREAM_UNAVAILABLE, Outcome.CLIENT_ERROR, False),
        (
            401,
            "UnrecognizedClientException",
            ErrorCode.UPSTREAM_UNAVAILABLE,
            Outcome.CLIENT_ERROR,
            False,
        ),
        (
            404,
            "ResourceNotFoundException",
            ErrorCode.UPSTREAM_UNAVAILABLE,
            Outcome.CLIENT_ERROR,
            False,
        ),
    ],
)
def test_errors_map_to_core_codes_and_outcomes(
    status: int, kind: str, code: ErrorCode, outcome: Outcome, free: bool
) -> None:
    resp = UpstreamResponse(
        status,
        {"x-amzn-ErrorType": f"{kind}:http://internal.amazon.com/", "Retry-After": "3"},
        b'{"message":"provider-text"}',
    )
    err = adapter().error(resp)
    assert err.code is code and err.status == status and err.sent
    assert err.outcome({429}) is outcome and err.not_processed({429}) is free
    assert err.retry_after_seconds == 3
    assert "provider-text" not in repr(err)


async def test_scripted_fake_errors_map_by_status() -> None:
    fake = FakeUpstream()
    fake.enqueue(Script(status=429, retry_after="2"), Script(status=503))
    up = adapter().translate(request({"messages": [USER]}), deployment())
    codes = []
    for _ in range(2):
        raw = await send(fake, up)
        codes.append(
            adapter().error(UpstreamResponse(raw.status_code, dict(raw.headers), raw.content)).code
        )
    assert codes == [ErrorCode.UPSTREAM_THROTTLED, ErrorCode.UPSTREAM_UNAVAILABLE]


def test_error_type_falls_back_to_the_body_type_field() -> None:
    resp = UpstreamResponse(400, {}, b'{"__type":"com.amazon#ValidationException","message":"x"}')
    assert adapter().error(resp).code is ErrorCode.INVALID_REQUEST


# --- relay: limits, usage chunk visibility, response shape ----------------------------------


def _full_stream() -> bytes:
    meta = {"usage": {"inputTokens": 5, "outputTokens": 6, "totalTokens": 11}}
    return b"".join(
        [
            eventstream_frame("messageStart", {"role": "assistant"}),
            eventstream_frame(
                "contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "Hi"}}
            ),
            eventstream_frame("messageStop", {"stopReason": "end_turn"}),
            eventstream_frame("metadata", meta),
        ]
    )


def test_the_usage_only_chunk_reaches_the_client_only_when_it_asked() -> None:
    plain = adapter().relay(converting({"messages": [USER]}, stream=True))
    shown = adapter().relay(
        converting({"messages": [USER], "stream_options": {"include_usage": True}}, stream=True)
    )
    quiet, loud = wire(plain.feed(_full_stream())), wire(shown.feed(_full_stream()))
    assert not any(dig(c, "usage") for c in chunks(quiet))
    assert dig(chunks(loud)[-1], "usage", "prompt_tokens") == 5
    assert plain.usage() == shown.usage() is not None  # settlement does not depend on the client


def test_relay_enforces_the_stream_limits() -> None:
    req = converting({"messages": [USER]}, stream=True)
    with pytest.raises(StreamLimitExceeded):
        adapter().relay(req, SseLimits(max_stream_bytes=64)).feed(_full_stream())
    with pytest.raises(StreamLimitExceeded):
        adapter().relay(req, SseLimits(max_events=2)).feed(_full_stream())
    with pytest.raises(StreamLimitExceeded):
        adapter().relay(req, SseLimits(max_event_bytes=40)).feed(_full_stream())


def test_relay_rejects_a_stream_cut_inside_a_frame_as_stream_broken() -> None:
    relay = adapter().relay(converting({"messages": [USER]}, stream=True))
    relay.feed(_full_stream()[:-3])
    with pytest.raises(StreamBroken):
        relay.close()


def test_an_unreadable_converse_answer_becomes_a_bodiless_502() -> None:
    out = adapter().response(converting({"messages": [USER]}), UpstreamResponse(200, {}, b"<html>"))
    assert (out.status, out.body) == (502, b"")
