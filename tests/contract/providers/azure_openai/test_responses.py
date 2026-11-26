"""Response side: usage, costs against the catalog, parity with an earlier guard, errors."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from spendgate.core.catalog import Catalog, parse
from spendgate.core.cost import Usage, actual_cost
from spendgate.core.errors import ErrorCode
from spendgate.core.money import Money
from spendgate.core.outcomes import Outcome
from spendgate.providers import openai_shapes as shapes
from spendgate.providers.azure_openai import AzureOpenAIProvider
from spendgate.providers.port import CleanRequest, SseEvent, UpstreamResponse
from spendgate.providers.sse import serialise
from tests.contract.providers.azure_openai.conftest import answer, fixture

REQ = CleanRequest(shapes.CHAT, "m", {})
PRICES = Path(__file__).parents[4] / "src/spendgate/catalog/prices.yaml"
M = Decimal(1_000_000)


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    doc = yaml.safe_load(PRICES.read_text())
    return parse([doc], "0" * 64, ledger_currency="USD")


def cost(catalog: Catalog, model: str, usage: Usage) -> Decimal:
    # The entry itself, not a lookup by date: retired models (gpt-4o-transcribe) are refused.
    entry = next(e for e in catalog.entries() if e.provider == "azure_openai" and e.model == model)
    return actual_cost(entry, usage).to_decimal()


def test_usage_ignores_azures_filter_annotations(provider: AzureOpenAIProvider) -> None:
    assert provider.usage(REQ, answer("chat_completion.json")) == Usage(
        input_tokens=1000, output_tokens=50, cached_input_tokens=100
    )


def test_stream_usage_and_the_injected_usage_chunk_is_hidden(provider: AzureOpenAIProvider) -> None:
    items = fixture("stream_events.json")
    assert isinstance(items, list)
    relay = provider.relay(CleanRequest(shapes.CHAT, "m", {"stream": True}, stream=True))
    out = b"".join(relay.feed(serialise(SseEvent(None, json.dumps(i)))) for i in items)
    assert relay.usage() == Usage(input_tokens=1000, output_tokens=50, cached_input_tokens=100)
    assert b'"usage": {' not in out  # the chunk the client never asked for
    assert b"prompt_filter_results" in out  # Azure's first chunk passes through


# Reference values from an earlier in-house guard's pricing tests (Global rates
# x 1.20 Data Zone), USD formulas, restated as numbers. Entry: (model, usage, earlier guard USD).
D = Decimal
U = Usage
PARITY = [
    # test_actual_cost_alpha_with_cache: (900*0.12 + 100*0.012 + 50*0.60) / 1e6
    (
        "gpt-6-luna",
        U(input_tokens=1000, cached_input_tokens=100, output_tokens=50),
        (D(900) * D("0.12") + 100 * D("0.012") + 50 * D("0.6")) / M,
    ),
    # test_actual_cost_sol: 1.40 * 1.2
    ("gpt-6.1-sol", U(input_tokens=200_000, output_tokens=100_000), D("1.40") * D("1.2")),
    # test_actual_cost_terra: (150k*1 + 50k*0.05 + 100k*5) / 1e6 * 1.2
    (
        "gpt-6.1-terra",
        U(input_tokens=200_000, cached_input_tokens=50_000, output_tokens=100_000),
        (D(150_000) + D(50_000) * D("0.05") + D(500_000)) / M * D("1.2"),
    ),
    # test_long_context...: (200k*4 + 100k*0.2 + 10k*15) / 1e6 * 1.2
    (
        "gpt-6.1-sol",
        U(input_tokens=300_000, cached_input_tokens=100_000, output_tokens=10_000),
        (D(800_000) + D(20_000) + D(150_000)) / M * D("1.2"),
    ),
    # threshold is strictly greater than 272k: 272000 * 0.12 / 1e6
    ("gpt-6-luna", U(input_tokens=272_000), D(272_000) * D("0.12") / M),
]


@pytest.mark.parametrize(("model", "usage", "expected"), PARITY)
def test_text_costs_equal_the_earlier_guard(
    catalog: Catalog, model: str, usage: Usage, expected: Decimal
) -> None:
    assert cost(catalog, model, usage) == expected


def test_transcription_costs_equal_the_earlier_guard(catalog: Catalog) -> None:
    # pricing.AUDIO_RATES usd_per_minute: billed minutes x rate (whole 60 s here).
    per_minute = {
        "whisper": Decimal("0.00792"),
        "gpt-transcribe": Decimal("0.00495"),
        "gpt-live-transcribe": Decimal("0.0187"),
    }
    for model, rate in per_minute.items():
        assert cost(catalog, model, Usage(audio_seconds=Decimal(60))) == rate
    # audio_tokens_cost_nano: (audio_in*a + text_in*t + text_out*o) / 1e6, USD per 1M tokens.
    tokens = {
        "gpt-4o-transcribe": (Decimal("7.92"), Decimal("3.30"), Decimal("13.20")),
        "gpt-4o-mini-transcribe": (Decimal("3.96"), Decimal("1.65"), Decimal("6.60")),
    }
    for model, (a, t, o) in tokens.items():
        usage = Usage(
            input_tokens=100, audio_input_tokens=1000, output_tokens=200
        )  # text and audio counted apart
        assert cost(catalog, model, usage) == (1000 * a + 100 * t + 200 * o) / M


NOW_MS = 1_795_651_200_000  # 2026-11-26
TEXT_MODES = ("chat", "responses")


def azure_text_routes() -> list[tuple[str, str | None]]:
    """Every bundled azure_openai chat or Responses id, with each Region it is scoped to (#106).

    A Region-less entry is priced for a deployment without a Region; a Region-scoped one for each
    Region it lists. Bare ids and generated `global.`/`datazone.`/`regional.` ids alike.
    """
    routes: set[tuple[str, str | None]] = set()
    for e in yaml.safe_load(PRICES.read_text())["models"]:
        if e["id"].startswith("azure_openai/") and e["mode"] in TEXT_MODES:
            model = e["id"].removeprefix("azure_openai/")
            routes |= {(model, r) for r in e.get("regions") or [None]}
    return sorted(routes, key=lambda r: (r[0], r[1] or ""))


@pytest.mark.parametrize(("model", "region"), azure_text_routes())
def test_usage_prices_through_every_bundled_azure_text_entry(
    catalog: Catalog, provider: AzureOpenAIProvider, model: str, region: str | None
) -> None:
    usage = provider.usage(REQ, answer("chat_completion.json"))
    assert usage == Usage(input_tokens=1000, output_tokens=50, cached_input_tokens=100)
    entry = catalog.lookup("azure_openai", model, NOW_MS, region)
    m = entry.meters
    assert m.input_per_mtok is not None and m.output_per_mtok is not None
    cached = m.cached_input_per_mtok if m.cached_input_per_mtok is not None else m.input_per_mtok
    expected = (900 * m.input_per_mtok + 100 * cached + 50 * m.output_per_mtok) / M
    # one ceiling to whole nano-USD at the end (catalog-schema.md section 5)
    assert actual_cost(entry, usage) == Money.from_amount(expected * entry.price_multiplier, "USD")


def test_content_filter_400_is_a_free_client_error(provider: AzureOpenAIProvider) -> None:
    err = provider.error(answer("error_content_filter.json", 400))
    assert (err.code, err.status) == (ErrorCode.INVALID_REQUEST, 400)
    assert err.outcome({429}) is Outcome.CLIENT_ERROR
    assert not err.not_processed({429})  # the filter ran: it is not a failover candidate


def test_429_retry_after_ms_is_converted_and_rounded_up(provider: AzureOpenAIProvider) -> None:
    for millis, seconds in (("7000", 7), ("7001", 8), ("250", 1), ("0", 0)):
        err = provider.error(answer("error_429.json", 429, {"Retry-After-Ms": millis}))
        assert (err.code, err.retry_after_seconds) == (ErrorCode.UPSTREAM_THROTTLED, seconds)
        assert err.not_processed({429})


def test_429_falls_back_to_retry_after_seconds_then_to_none(provider: AzureOpenAIProvider) -> None:
    assert (
        provider.error(answer("error_429.json", 429, {"retry-after": "7"})).retry_after_seconds == 7
    )
    assert (
        provider.error(answer("error_429.json", 429, {"retry-after": "7.2"})).retry_after_seconds
        == 8
    )
    both = {"retry-after": "9", "retry-after-ms": "3000"}
    assert provider.error(answer("error_429.json", 429, both)).retry_after_seconds == 3
    assert (
        provider.error(answer("error_429.json", 429, {"retry-after-ms": "x"})).retry_after_seconds
        is None
    )
    assert provider.error(answer("error_429.json", 429)).retry_after_seconds is None


def test_other_errors_use_the_openai_mapping(provider: AzureOpenAIProvider) -> None:
    unauthorized = provider.error(answer("error_429.json", 401))
    assert unauthorized.code is ErrorCode.UPSTREAM_UNAVAILABLE  # our key is wrong, not the client
    assert provider.error(answer("error_429.json", 500)).code is ErrorCode.UPSTREAM_UNAVAILABLE
    # retry-after-ms on a non-throttle answer is ignored
    err = provider.error(answer("error_content_filter.json", 400, {"retry-after-ms": "5000"}))
    assert err.retry_after_seconds is None


def test_speech_usage_is_billed_by_characters_on_success_only(
    provider: AzureOpenAIProvider,
) -> None:
    speech = CleanRequest(shapes.SPEECH, "m", {"input": "héllo", "voice": "alloy"})
    assert provider.usage(speech, UpstreamResponse(200, {}, b"ID3")) == Usage(characters=5)
    assert provider.usage(speech, UpstreamResponse(500, {}, b"")) is None
