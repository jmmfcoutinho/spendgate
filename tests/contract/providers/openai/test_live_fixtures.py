"""Answers recorded from the live OpenAI API on 2026-11-25 (issue #9), read as the gateway does.

The hand-written fixtures next to these keep the edge cases (cached, audio, reasoning splits);
these check that the real shapes, with their extra fields, give the usage the provider billed.
"""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from spendgate.core.catalog import Catalog, parse
from spendgate.core.cost import Usage, actual_cost
from spendgate.providers import openai_shapes as shapes
from spendgate.providers.openai import OpenAIProvider
from spendgate.providers.port import CleanRequest, Json
from tests.contract.providers.openai.conftest import FIXTURES, answer

PRICES = Path(__file__).parents[4] / "src/spendgate/catalog/prices.yaml"
RECORDED_MS = int(datetime(2026, 11, 25, tzinfo=UTC).timestamp()) * 1000
ASKED: dict[str, Json] = {"stream": True, "stream_options": {"include_usage": True}}


def relay_bytes(
    provider: OpenAIProvider, endpoint: str, name: str, body: dict[str, Json]
) -> tuple[bytes, bytes, Usage | None]:
    """(recorded bytes, bytes the client gets, usage the relay read) for one recorded stream."""
    relay = provider.relay(CleanRequest(endpoint, "m", body, stream=True))
    raw = (FIXTURES / name).read_bytes()
    return raw, relay.feed(raw) + relay.close(), relay.usage()


@pytest.mark.parametrize(
    ("endpoint", "name", "usage"),
    [
        (shapes.CHAT, "chat_completion_live.json", Usage(13, 16, reasoning_tokens=6)),
        (shapes.RESPONSES, "responses_live.json", Usage(13, 5)),
        (shapes.EMBEDDINGS, "embeddings_live.json", Usage(input_tokens=4)),
    ],
)
def test_live_answers_give_the_billed_usage(
    provider: OpenAIProvider, endpoint: str, name: str, usage: Usage
) -> None:
    assert provider.usage(CleanRequest(endpoint, "m", {}), answer(name)) == usage


def test_live_chat_stream_hides_the_injected_usage_chunk_and_reads_it(
    provider: OpenAIProvider,
) -> None:
    raw, out, usage = relay_bytes(provider, shapes.CHAT, "chat_stream_live.sse", {"stream": True})
    assert usage == Usage(13, 15, reasoning_tokens=5)
    assert b'"content":"OK"' in out and out.endswith(b"data: [DONE]\n\n")
    assert b'"prompt_tokens"' not in out and b'"prompt_tokens"' in raw


def test_live_chat_stream_is_relayed_unchanged_when_the_client_asked_for_usage(
    provider: OpenAIProvider,
) -> None:
    raw, out, usage = relay_bytes(provider, shapes.CHAT, "chat_stream_live.sse", ASKED)
    assert out == raw and usage == Usage(13, 15, reasoning_tokens=5)


def test_live_responses_stream_is_relayed_unchanged_and_completed_gives_usage(
    provider: OpenAIProvider,
) -> None:
    body: dict[str, Json] = {"stream": True}
    raw, out, usage = relay_bytes(provider, shapes.RESPONSES, "responses_stream_live.sse", body)
    assert out == raw and usage == Usage(13, 17, reasoning_tokens=10)


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return parse([yaml.safe_load(PRICES.read_text())], "0" * 64, ledger_currency="USD")


@pytest.mark.parametrize(
    "case",
    [
        (shapes.CHAT, "gpt-6-luna", "chat_completion_live.json", "0.0000093"),  # 13x0.1 + 16x0.5
        (shapes.RESPONSES, "gpt-6-luna", "responses_live.json", "0.0000038"),  # 13x0.1 + 5x0.5
        (shapes.EMBEDDINGS, "text-embedding-3-small", "embeddings_live.json", "0.00000008"),
    ],
)
def test_live_answers_cost_their_tokens_at_the_catalog_price(
    provider: OpenAIProvider, catalog: Catalog, case: tuple[str, str, str, str]
) -> None:
    endpoint, model, name, usd = case
    usage = provider.usage(CleanRequest(endpoint, "m", {}), answer(name))
    assert usage is not None
    entry = catalog.lookup("openai", model, RECORDED_MS)
    assert actual_cost(entry, usage).to_decimal() == Decimal(usd)
