"""Answers recorded from the live Anthropic API on 2026-11-25 (issue #9), read as the gateway does.

The documented fixtures next to these keep the cache and last-delta edge cases; these check the
real shapes (cumulative counts repeated on `message_delta`, `output_tokens_details`, padded SSE
data lines) give the usage Anthropic billed.
"""

from datetime import UTC, datetime
from decimal import Decimal
from importlib import resources
from pathlib import Path

import pytest
import yaml

from spendgate.core.catalog import parse
from spendgate.core.cost import Usage, actual_cost
from spendgate.providers.anthropic import AnthropicProvider
from spendgate.providers.port import CleanRequest, Json, UpstreamResponse

FIXTURES = Path(__file__).parent / "fixtures"
PROVIDER = AnthropicProvider()
RECORDED_MS = int(datetime(2026, 11, 25, tzinfo=UTC).timestamp()) * 1000
BODY: dict[str, Json] = {
    "model": "m",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hi"}],
}
REQ = CleanRequest("/v1/messages", "m", BODY)


@pytest.mark.parametrize(
    ("name", "usage"),
    [
        ("message_response_live_20261125.json", Usage(18, 4)),
        ("message_tool_use_live_20261125.json", Usage(380, 52)),
    ],
)
def test_live_answers_give_the_billed_usage(name: str, usage: Usage) -> None:
    body = (FIXTURES / name).read_bytes()
    assert PROVIDER.usage(REQ, UpstreamResponse(200, {}, body)) == usage


def test_live_stream_is_relayed_byte_for_byte_and_gives_the_billed_usage() -> None:
    provenance, recorded = (FIXTURES / "stream_live_20261125.sse").read_bytes().split(b"\n\n", 1)
    assert provenance.startswith(b": ")  # the fixture's own note, a comment the relay drops
    relay = PROVIDER.relay(CleanRequest("/v1/messages", "m", {**BODY, "stream": True}, stream=True))
    assert relay.feed(provenance + b"\n\n" + recorded) + relay.close() == recorded
    assert relay.usage() == Usage(18, 4)


@pytest.mark.parametrize(
    ("name", "usd"),
    [
        ("message_response_live_20261125.json", "0.0000038"),  # 18 x 0.1 + 4 x 0.5 per Mtok
        ("message_tool_use_live_20261125.json", "0.000064"),  # 380 x 0.1 + 52 x 0.5 per Mtok
    ],
)
def test_live_answers_cost_their_tokens_at_the_catalog_price(name: str, usd: str) -> None:
    text = (resources.files("spendgate.catalog") / "prices.yaml").read_text(encoding="utf-8")
    catalog = parse([yaml.safe_load(text)], "0" * 64, ledger_currency="USD")
    usage = PROVIDER.usage(REQ, UpstreamResponse(200, {}, (FIXTURES / name).read_bytes()))
    assert usage is not None
    entry = catalog.lookup("anthropic", "claude-haiku-5-5", RECORDED_MS)
    assert actual_cost(entry, usage).to_decimal() == Decimal(usd)
