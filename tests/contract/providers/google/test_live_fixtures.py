"""Answers recorded live on 2026-11-25 (issue #9) from the Gemini API and Vertex AI.

The hand-written fixtures next to these keep the edge cases (thinking and cached counts, a call
without an id); these check the real shapes convert to OpenAI chat answers and give the usage
Google billed. Real differences they cover: thought signatures on text parts, `functionCall.id`,
Vertex's `trafficType` and a first stream chunk whose `usageMetadata` has no counts, and the
Gemini API's running totals on every stream chunk.
"""

import json
from decimal import Decimal

import pytest

from spendgate.core.cost import Usage, actual_cost
from spendgate.providers.port import UpstreamResponse
from tests.contract.providers.google.helpers import (
    USER,
    catalog,
    converting,
    dig,
    fixture,
    provider,
    request,
    wire,
)

REQ = request({"messages": [USER]})
ASKED = {"messages": [USER], "stream": True, "stream_options": {"include_usage": True}}
RECORDED_MS = 1_795_564_800_000  # 2026-11-25T00:00Z


@pytest.mark.parametrize(
    ("name", "usage"),
    [
        ("gemini_text_live_20261125.json", Usage(8, 1)),
        ("vertex_text_live_20261125.json", Usage(7, 1)),
    ],
)
def test_live_text_answer_converts_and_gives_the_billed_usage(name: str, usage: Usage) -> None:
    raw = fixture(name)
    body = json.loads(provider().response(converting(), UpstreamResponse(200, {}, raw)).body)
    assert dig(body, "choices", 0, "message") == {"role": "assistant", "content": "OK"}
    assert dig(body, "choices", 0, "finish_reason") == "stop"
    assert dig(body, "usage", "prompt_tokens") == usage.input_tokens
    assert dig(body, "usage", "completion_tokens") == usage.output_tokens
    assert provider().usage(REQ, UpstreamResponse(200, {}, raw)) == usage


@pytest.mark.parametrize(
    ("name", "usage"),
    [
        ("gemini_function_call_live_20261125.json", Usage(53, 17)),
        ("vertex_function_call_live_20261125.json", Usage(32, 17)),
    ],
)
def test_live_function_call_keeps_googles_call_id_and_signature(name: str, usage: Usage) -> None:
    raw = fixture(name)
    choice = json.loads(provider().response(converting(), UpstreamResponse(200, {}, raw)).body)
    assert dig(choice, "choices", 0, "finish_reason") == "tool_calls"
    call = dig(choice, "choices", 0, "message", "tool_calls", 0)
    recorded = dig(json.loads(raw), "candidates", 0, "content", "parts", 0)
    assert dig(call, "id") == dig(recorded, "functionCall", "id")
    assert dig(call, "function", "name") == "get_weather"
    assert json.loads(str(dig(call, "function", "arguments"))) == {"city": "Lisbon"}
    signature = dig(call, "extra_content", "google", "thought_signature")
    assert signature == dig(recorded, "thoughtSignature")
    assert provider().usage(REQ, UpstreamResponse(200, {}, raw)) == usage


@pytest.mark.parametrize(
    ("name", "usage"),
    [
        ("gemini_stream_live_20261125.sse", Usage(8, 1)),
        ("vertex_stream_live_20261125.sse", Usage(7, 1)),
    ],
)
def test_live_stream_converts_to_chunks_with_the_billed_final_usage(
    name: str, usage: Usage
) -> None:
    relay = provider().relay(converting(ASKED, stream=True))
    events = wire(relay.feed(fixture(name)) + relay.close())
    assert relay.usage() == usage
    assert events[-1].data == "[DONE]"
    chunks = [json.loads(e.data) for e in events[:-1]]
    text = "".join(str(dig(c, "choices", 0, "delta", "content") or "") for c in chunks)
    assert text == "OK"
    assert dig(chunks[-1], "choices") == []
    assert dig(chunks[-1], "usage", "prompt_tokens") == usage.input_tokens
    assert dig(chunks[-1], "usage", "completion_tokens") == usage.output_tokens
    assert sum(1 for c in chunks if dig(c, "usage")) == 1  # no running total reaches the client


@pytest.mark.parametrize(
    ("name", "usd"),
    [
        ("gemini_text_live_20261125.json", "0.0000049"),  # 8 x 0.3 + 1 x 2.5 per Mtok
        ("gemini_function_call_live_20261125.json", "0.0000584"),  # 53 x 0.3 + 17 x 2.5
        ("vertex_text_live_20261125.json", "0.0000046"),  # Vertex is priced as the Gemini API
        ("vertex_function_call_live_20261125.json", "0.0000521"),
    ],
)
def test_live_answers_cost_their_tokens_at_the_catalog_price(name: str, usd: str) -> None:
    usage = provider().usage(REQ, UpstreamResponse(200, {}, fixture(name)))
    assert usage is not None
    entry = catalog().lookup("google", "gemini-3.5-flash-lite", RECORDED_MS)
    assert actual_cost(entry, usage).to_decimal() == Decimal(usd)
