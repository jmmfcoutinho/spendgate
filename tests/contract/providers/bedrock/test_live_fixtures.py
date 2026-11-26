"""Answers recorded live on 2026-11-26 (issue #9) from Bedrock Converse in eu-west-1.

The hand-built frames in `test_responses.py` keep the edge cases (cache counts, exception frames,
corrupt CRCs); these check that real answers convert to OpenAI chat answers and give the usage
Bedrock billed. Real details they cover: a `type` field on `toolUse` blocks, stream payloads padded
with a `p` field, no `contentBlockStart` before a text delta, and a stream `metadata.usage` without
cache counts (and with `serverToolUsage`) where the non-stream usage has both cache counts at 0.
"""

import json
from decimal import Decimal
from pathlib import Path

from spendgate.core.cost import Usage, actual_cost
from spendgate.providers.port import UpstreamResponse
from tests.contract.providers.bedrock.helpers import adapter, converting, dig, request, wire
from tests.contract.providers.bedrock.test_responses import catalog, chunks

FIXTURES = Path(__file__).parent / "fixtures"
USER = {"role": "user", "content": "Reply with the single word OK."}
REQ = request({"messages": [USER]})
RECORDED_MS = 1_795_651_200_000  # 2026-11-26T00:00Z
HAIKU = "anthropic.claude-haiku-5-5"  # the catalog id the eu. inference profile was priced with


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_live_text_answer_converts_and_gives_the_billed_usage() -> None:
    raw = fixture("converse_text_live_20261126.json")
    body = json.loads(
        adapter().response(converting({"messages": [USER]}), UpstreamResponse(200, {}, raw)).body
    )
    assert dig(body, "choices", 0, "message") == {"role": "assistant", "content": "OK"}
    assert dig(body, "choices", 0, "finish_reason") == "stop"
    assert (
        dig(body, "usage", "prompt_tokens") == 18 and dig(body, "usage", "completion_tokens") == 4
    )
    assert adapter().usage(REQ, UpstreamResponse(200, {}, raw)) == Usage(18, 4)


def test_live_tool_use_becomes_a_tool_call_with_bedrocks_id() -> None:
    raw = fixture("converse_tool_live_20261126.json")
    body = json.loads(
        adapter().response(converting({"messages": [USER]}), UpstreamResponse(200, {}, raw)).body
    )
    choice = dig(body, "choices", 0)
    assert (
        dig(choice, "finish_reason") == "tool_calls" and dig(choice, "message", "content") is None
    )
    call = dig(choice, "message", "tool_calls", 0)
    assert dig(call, "id") == "tooluse_FIXTURE00000000000001"
    assert dig(call, "function", "name") == "get_weather"
    assert json.loads(str(dig(call, "function", "arguments"))) == {"city": "Lisbon"}
    assert adapter().usage(REQ, UpstreamResponse(200, {}, raw)) == Usage(380, 52)


def test_live_event_stream_bytes_decode_to_openai_chunks_and_the_billed_usage() -> None:
    raw = fixture("converse_stream_live_20261126.bin")
    assert len(raw) == 880  # the exact bytes Bedrock sent, CRCs included
    body = {"messages": [USER], "stream": True, "stream_options": {"include_usage": True}}
    stream = adapter().relay(converting(body, stream=True))
    out = b""
    for i in range(0, len(raw), 13):  # arbitrary network splits, also inside a prelude
        out += stream.feed(raw[i : i + 13])
    out += stream.close()
    events = wire(out)
    parsed = chunks(events)
    text = "".join(str(dig(c, "choices", 0, "delta", "content") or "") for c in parsed)
    assert text == "OK" and events[-1].data == "[DONE]"
    assert dig(parsed[-2], "choices", 0, "finish_reason") == "stop"
    assert dig(parsed[-1], "usage", "prompt_tokens") == 18
    assert all(
        '"p"' not in e.data for e in events
    )  # Bedrock's padding field never reaches the client
    assert stream.usage() == Usage(18, 4)


def test_live_stream_and_non_stream_give_the_same_usage_and_cost() -> None:
    text = adapter().usage(
        REQ, UpstreamResponse(200, {}, fixture("converse_text_live_20261126.json"))
    )
    stream = adapter().relay(request({"messages": [USER]}, stream=True))
    stream.feed(fixture("converse_stream_live_20261126.bin"))
    stream.close()
    assert stream.usage() == text


def test_live_usage_prices_at_the_bundled_haiku_entry() -> None:
    entry = catalog().lookup("bedrock", HAIKU, RECORDED_MS)
    m = entry.meters
    for name, usage in (
        ("converse_text_live_20261126.json", Usage(18, 4)),
        ("converse_tool_live_20261126.json", Usage(380, 52)),
    ):
        billed = adapter().usage(REQ, UpstreamResponse(200, {}, fixture(name)))
        assert billed == usage
        expected = (
            Decimal(usage.input_tokens) * m.input_per_mtok  # type: ignore[operator]
            + Decimal(usage.output_tokens) * m.output_per_mtok  # type: ignore[operator]
        ) / 1_000_000
        assert actual_cost(entry, billed).nano == int(expected * 10**9)
    # The smoke run's own numbers (USD, before FX 0.95): 4.18 and 70.4 micro-dollars.
    assert actual_cost(entry, Usage(18, 4)).nano == 4180
    assert actual_cost(entry, Usage(380, 52)).nano == 70400
