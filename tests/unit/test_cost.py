"""Actual and worst-case cost: golden values from an earlier guard, long context, rounding."""

from decimal import ROUND_CEILING, Decimal

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from spendgate.core.catalog import Catalog, PriceEntry, parse
from spendgate.core.cost import Estimate, UnpricedUsage, Usage, actual_cost, worst_case
from spendgate.core.money import FxSnapshot, Money

NOW = 1_794_000_000_000  # 2026-11-07, after every effective_from below
FX = Decimal("0.92")  # the earlier guard used the same FX value
DZ = Decimal("1.2")

# The earlier guard's three models: Data Zone rates are the Global rates x 1.20 (pricing.RATES).
# Whisper and the gpt-4o-transcribe family are the pricing.AUDIO_RATES rows.
GOLDEN_YAML = """
catalog_version: 1
models:
  - &alpha
    id: azure_openai/gpt-6-luna
    mode: responses
    currency: USD
    effective_from: "2026-01-01"
    source_url: https://example.com/p
    verified: false
    deployment_type: data_zone
    price_multiplier: "1.2"
    meters: {input_per_mtok: "0.1", cached_input_per_mtok: "0.01", output_per_mtok: "0.5"}
    long_context: {threshold_input_tokens: 272000, input_multiplier: "2", output_multiplier: "1.5"}
    limits: {max_output_tokens: 64000}
  - id: azure_openai/gpt-6.1-terra
    mode: responses
    currency: USD
    effective_from: "2026-01-01"
    source_url: https://example.com/p
    verified: false
    price_multiplier: "1.2"
    meters: {input_per_mtok: "1.00", cached_input_per_mtok: "0.05", output_per_mtok: "5.00"}
    long_context: {threshold_input_tokens: 272000, input_multiplier: "2", output_multiplier: "1.5"}
    limits: {max_output_tokens: 64000}
  - id: azure_openai/gpt-6.1-sol
    mode: responses
    currency: USD
    effective_from: "2026-01-01"
    source_url: https://example.com/p
    verified: false
    price_multiplier: "1.2"
    meters: {input_per_mtok: "2", cached_input_per_mtok: "0.1", output_per_mtok: "10"}
    long_context: {threshold_input_tokens: 272000, input_multiplier: "2", output_multiplier: "1.5"}
    limits: {max_output_tokens: 64000}
  - id: azure_openai/whisper
    mode: transcription
    currency: USD
    effective_from: "2026-01-01"
    source_url: https://example.com/p
    verified: false
    meters: {audio_input_per_minute: "0.00792"}
  - id: azure_openai/gpt-4o-transcribe
    mode: transcription
    currency: USD
    effective_from: "2026-01-01"
    source_url: https://example.com/p
    verified: false
    meters:
      audio_input_per_minute: "0.00792"
      audio_input_per_mtok: "7.92"
      input_per_mtok: "3.30"
      output_per_mtok: "13.20"
  - id: elevenlabs/eleven_flash_v2_5
    mode: speech
    currency: USD
    effective_from: "2026-01-01"
    source_url: https://example.com/p
    verified: false
    meters: {per_1k_characters: "0.05"}
"""


def golden() -> Catalog:
    fx = FxSnapshot.of("USD", "EUR", FX, "2026-11-13", "earlier guard test value")
    return parse([yaml.safe_load(GOLDEN_YAML)], "00" * 32, ledger_currency="EUR", fx=(fx,))


def ledger_nano(model: str, usage: Usage) -> int:
    catalog = golden()
    provider, _, name = model.partition("/")
    entry = catalog.lookup(provider, name, NOW)
    return catalog.in_ledger(actual_cost(entry, usage)).nano


def worst_nano(model: str, est: Estimate) -> int:
    catalog = golden()
    provider, _, name = model.partition("/")
    return catalog.in_ledger(worst_case(catalog.lookup(provider, name, NOW), est)).nano


def reference_nano(usd: Decimal) -> int:
    """The earlier guard's `_to_nano`: one ceiling on USD x FX x 1e9."""
    return int((usd * FX * 10**9).to_integral_value(rounding=ROUND_CEILING))


Alpha = "azure_openai/gpt-6-luna"
TERRA = "azure_openai/gpt-6.1-terra"
SOL = "azure_openai/gpt-6.1-sol"


# --- golden values from the earlier guard's pricing and transcription tests ---------------


def test_golden_actual_cost_alpha_with_cache() -> None:
    # test_actual_cost_alpha_with_cache: actual_cost_nano("gpt-6-luna", 1000, 100, 50, FX)
    usage = Usage(input_tokens=1000, cached_input_tokens=100, output_tokens=50)
    assert ledger_nano(Alpha, usage) == 128_064


def test_golden_actual_cost_sol() -> None:
    # test_actual_cost_sol: 200_000 in, 100_000 out -> EUR 1.40 x 1.2 x 0.92
    nano = ledger_nano(SOL, Usage(input_tokens=200_000, output_tokens=100_000))
    assert nano == reference_nano(Decimal("1.40") * DZ) == 1_545_600_000


def test_golden_actual_cost_terra() -> None:
    # test_actual_cost_terra: 200k in (50k cached) + 100k out
    nano = ledger_nano(
        TERRA, Usage(input_tokens=200_000, cached_input_tokens=50_000, output_tokens=100_000)
    )
    usd = (Decimal(150_000) + Decimal(50_000) * Decimal("0.05") + Decimal(100_000) * 5) / 10**6
    assert nano == reference_nano(usd * DZ) == 720_360_000


def test_golden_long_context_doubles_input_and_cached_and_scales_output() -> None:
    # test_long_context_doubles_...: Sol, 300k in (100k cached), 10k out
    usage = Usage(input_tokens=300_000, cached_input_tokens=100_000, output_tokens=10_000)
    usd = (Decimal(200_000) * 4 + Decimal(100_000) * Decimal("0.2") + Decimal(10_000) * 15) / 10**6
    assert ledger_nano(SOL, usage) == reference_nano(usd * DZ) == 1_070_880_000


def test_golden_threshold_is_strictly_greater_than_272k() -> None:
    # test_threshold_is_strictly_greater_than_272k
    assert ledger_nano(Alpha, Usage(input_tokens=272_000)) == 30_028_800
    over = ledger_nano(Alpha, Usage(input_tokens=272_001))
    assert over > 30_028_800 * 2 - 1_000_000
    assert over == reference_nano(Decimal(272_001) * Decimal("0.24") / 10**6) == 60_057_821


def test_golden_worst_case_ignores_cache_and_applies_long_factors() -> None:
    # test_worst_case_ignores_cache_and_applies_long_factors
    short = worst_nano(Alpha, Estimate(input_tokens=1000, max_output_tokens=2000))
    assert short == reference_nano(
        (Decimal(1000) * Decimal("0.1") + 2000 * Decimal("0.5")) * DZ / 10**6
    )
    long = worst_nano(SOL, Estimate(input_tokens=300_000, max_output_tokens=1000))
    assert long == reference_nano((Decimal(300_000) * 4 + 1000 * 15) / 10**6 * DZ) == 1_341_360_000


def test_golden_costs_round_up() -> None:
    # test_costs_round_up: 1.104e-7 EUR rounds up to 111 nano
    assert ledger_nano(Alpha, Usage(input_tokens=1)) == 111


def test_golden_whisper_one_minute_reservation() -> None:
    # test_audio_rates...: audio_reservation_nano("whisper", 60_000, 0.92) == 7_286_400
    assert ledger_nano("azure_openai/whisper", Usage(audio_seconds=Decimal(60))) == 7_286_400


def test_golden_billed_seconds_round_up_with_a_minimum_of_one() -> None:
    # test_audio_rates...: billed_seconds(1) == 1, (1001 ms) == 2, (0) == 1
    one_second = ledger_nano("azure_openai/whisper", Usage(audio_seconds=Decimal(1)))
    assert ledger_nano("azure_openai/whisper", Usage(audio_seconds=Decimal("0.001"))) == one_second
    two_seconds = ledger_nano("azure_openai/whisper", Usage(audio_seconds=Decimal(2)))
    assert ledger_nano("azure_openai/whisper", Usage(audio_seconds=Decimal("1.001"))) == two_seconds
    assert two_seconds == reference_nano(Decimal(2) / 60 * Decimal("0.00792"))


def test_golden_audio_token_cost() -> None:
    # pricing.audio_tokens_cost_nano("gpt-4o-transcribe", audio_in, text_in, text_out, FX)
    usage = Usage(audio_input_tokens=1200, input_tokens=300, output_tokens=450)
    usd = (Decimal(1200) * Decimal("7.92") + 300 * Decimal("3.30") + 450 * Decimal("13.20")) / 10**6
    assert ledger_nano("azure_openai/gpt-4o-transcribe", usage) == reference_nano(usd)


def test_golden_elevenlabs_per_1k_characters() -> None:
    # pricing.ELEVEN_RATES is USD per 1,000 chars; 2,500 chars at 0.05 = 0.125 USD
    nano = ledger_nano("elevenlabs/eleven_flash_v2_5", Usage(characters=2500))
    assert nano == reference_nano(Decimal("0.125"))


def test_schema_worked_examples() -> None:
    # docs/catalog-schema.md section 5, Example 1: Alpha, 10,000 in (4,000 cached), 2,000 out
    usage = Usage(input_tokens=10_000, cached_input_tokens=4_000, output_tokens=2_000)
    fx = FxSnapshot.of("USD", "EUR", "0.9137", "2026-11-13", "doc example")
    catalog = parse([yaml.safe_load(GOLDEN_YAML)], "00" * 32, ledger_currency="EUR", fx=(fx,))
    entry = catalog.lookup("azure_openai", "gpt-6-luna", NOW)
    assert catalog.in_ledger(actual_cost(entry, usage)).nano == 1_798_162
    # Example 2: whisper, 95.3 s -> 96 s
    whisper = catalog.lookup("azure_openai", "whisper", NOW)
    assert catalog.in_ledger(actual_cost(whisper, Usage(audio_seconds=Decimal("95.3")))).nano == (
        11_578_407
    )


# --- rules --------------------------------------------------------------------------------------


def parsed(*entries: dict[str, object]) -> PriceEntry:
    base = {
        "mode": "chat",
        "currency": "USD",
        "effective_from": "2026-01-01",
        "source_url": "https://example.com/p",
        "verified": False,
        "limits": {"max_output_tokens": 1000},
    }
    models = [{"id": "openai/m"} | base | e for e in entries]
    return parse(
        [{"catalog_version": 1, "models": models}], "00" * 32, ledger_currency="USD"
    ).lookup("openai", "m", NOW)


def test_single_ceiling_at_the_end_not_per_meter() -> None:
    # Each token costs 0.1 nano: per-meter ceilings would book 2 nano, one ceiling on the sum 1.
    e = parsed({"meters": {"input_per_mtok": "0.0001", "output_per_mtok": "0.0001"}})
    assert actual_cost(e, Usage(input_tokens=1, output_tokens=1)) == Money(1, "USD")


def test_cached_and_cache_write_fall_back_to_the_input_rate_never_free() -> None:
    e = parsed({"meters": {"input_per_mtok": "2", "output_per_mtok": "8"}})
    usage = Usage(input_tokens=1000, cached_input_tokens=400, cache_write_tokens=100)
    assert actual_cost(e, usage) == Money(2_000_000, "USD")  # all 1000 at 2 USD / Mtok


def test_cache_write_has_its_own_meter() -> None:
    e = parsed(
        {"meters": {"input_per_mtok": "2", "output_per_mtok": "8", "cache_write_per_mtok": "4"}}
    )
    usage = Usage(input_tokens=1000, cache_write_tokens=500)
    assert actual_cost(e, usage) == Money(3_000_000, "USD")  # 500 x 2 + 500 x 4


def test_reasoning_stays_inside_output_unless_it_has_its_own_meter() -> None:
    plain = parsed({"meters": {"input_per_mtok": "1", "output_per_mtok": "10"}})
    split = parsed(
        {"meters": {"input_per_mtok": "1", "output_per_mtok": "10", "reasoning_per_mtok": "30"}}
    )
    usage = Usage(output_tokens=1000, reasoning_tokens=600)
    assert actual_cost(plain, usage) == Money(10_000_000, "USD")
    assert actual_cost(split, usage) == Money(22_000_000, "USD")  # 400 x 10 + 600 x 30


def test_over_reported_subsets_are_billed_in_full_never_under_stated() -> None:
    e = parsed(
        {"meters": {"input_per_mtok": "2", "cached_input_per_mtok": "1", "output_per_mtok": "8"}}
    )
    assert actual_cost(e, Usage(input_tokens=100, cached_input_tokens=500)) == Money(500_000, "USD")


def test_audio_granularity_rounds_up_to_the_unit() -> None:
    e = parsed(
        {
            "mode": "transcription",
            "meters": {"audio_input_per_minute": "6", "audio_granularity_seconds": 15},
        }
    )
    # 16 s -> 30 s = 0.5 min x 6 = 3 USD
    assert actual_cost(e, Usage(audio_seconds=Decimal(16))) == Money(3 * 10**9, "USD")
    assert actual_cost(e, Usage(audio_seconds=Decimal(0))) == Money(0, "USD")


def test_images_and_price_multiplier() -> None:
    e = parsed(
        {
            "mode": "chat",
            "meters": {"input_per_mtok": "1", "output_per_mtok": "1", "per_image": "0.01"},
            "price_multiplier": "1.5",
        }
    )
    assert actual_cost(e, Usage(images=4)) == Money(60_000_000, "USD")
    assert worst_case(e, Estimate(images=4)) == Money(60_000_000, "USD")


def test_usage_on_a_meter_the_entry_does_not_price_is_refused_not_free() -> None:
    e = parsed({"meters": {"input_per_mtok": "1", "output_per_mtok": "1"}})
    for usage in (Usage(images=1), Usage(audio_seconds=Decimal(3)), Usage(characters=5),
                  Usage(audio_input_tokens=1), Usage(audio_output_tokens=1)):  # fmt: skip
        with pytest.raises(UnpricedUsage):
            actual_cost(e, usage)
    for est in (Estimate(images=1), Estimate(audio_seconds=Decimal(1)), Estimate(characters=1),
                Estimate(audio_input_tokens=1)):  # fmt: skip
        with pytest.raises(UnpricedUsage):
            worst_case(e, est)


def test_usage_and_estimate_reject_floats_bools_and_negatives() -> None:
    with pytest.raises(TypeError):
        Usage(input_tokens=1.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Usage(images=True)
    with pytest.raises(TypeError):
        Usage(audio_seconds=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="negative"):
        Usage(output_tokens=-1)
    with pytest.raises(ValueError, match="negative"):
        Estimate(max_output_tokens=-1)


# --- long context -------------------------------------------------------------------------------

LONG = {
    "meters": {
        "input_per_mtok": "10",
        "cached_input_per_mtok": "1",
        "output_per_mtok": "20",
        "audio_input_per_mtok": "5",
        "per_image": "1",
    },
    "long_context": {
        "threshold_input_tokens": 1000,
        "input_multiplier": "2",
        "output_multiplier": "3",
    },
}


def test_long_context_applies_to_input_cached_audio_and_output_but_not_per_unit_meters() -> None:
    e = parsed(LONG)
    below = Usage(input_tokens=1000, output_tokens=100, images=2, audio_input_tokens=10)
    above = Usage(input_tokens=1001, output_tokens=100, images=2, audio_input_tokens=10)
    base = Decimal(1000 * 10 + 100 * 20 + 10 * 5) / 10**6 + 2
    assert actual_cost(e, below) == Money.from_amount(base, "USD")
    long_cost = Decimal(1001 * 10 * 2 + 100 * 20 * 3 + 10 * 5 * 2) / 10**6 + 2
    assert actual_cost(e, above) == Money.from_amount(long_cost, "USD")


def test_worst_case_takes_the_long_tier_when_the_estimate_is_within_the_margin() -> None:
    e = parsed(LONG)  # threshold 1000: 800 x 1.25 reaches it (M-14)
    below = worst_case(e, Estimate(input_tokens=799, max_output_tokens=100))
    near = worst_case(e, Estimate(input_tokens=800, max_output_tokens=100))
    assert below == Money.from_amount(Decimal(799 * 10 + 100 * 20) / 10**6, "USD")
    assert near == Money.from_amount(Decimal(800 * 10 * 2 + 100 * 20 * 3) / 10**6, "USD")


def test_a_multiplier_below_one_never_shrinks_the_worst_case() -> None:
    cheap = {
        "long_context": {
            "threshold_input_tokens": 10,
            "input_multiplier": "0.5",
            "output_multiplier": "0.5",
        },
        "meters": {"input_per_mtok": "10", "output_per_mtok": "10"},
    }
    e = parsed(cheap)
    # Actual at 11 input tokens is cheaper, but the request could have stayed in the short tier.
    assert worst_case(e, Estimate(input_tokens=11, max_output_tokens=10)) == Money(210_000, "USD")
    assert actual_cost(e, Usage(input_tokens=11, output_tokens=10)) == Money(105_000, "USD")


def test_no_long_context_block_means_no_tier() -> None:
    e = parsed({"meters": {"input_per_mtok": "1", "output_per_mtok": "1"}})
    assert actual_cost(e, Usage(input_tokens=10**7)) == Money(10 * 10**9, "USD")


# --- worst case ---------------------------------------------------------------------------------


def test_worst_case_bills_every_input_token_at_the_dearest_input_meter() -> None:
    e = parsed(
        {
            "meters": {
                "input_per_mtok": "2",
                "cache_write_per_mtok": "5",
                "cached_input_per_mtok": "1",
                "output_per_mtok": "8",
                "reasoning_per_mtok": "30",
            }
        }
    )
    got = worst_case(e, Estimate(input_tokens=1000, max_output_tokens=100))
    assert got == Money.from_amount(Decimal(1000 * 5 + 100 * 30) / 10**6, "USD")


def test_worst_case_audio_uses_the_dearer_of_minute_and_token_meters() -> None:
    meters = {"audio_input_per_minute": "1", "audio_input_per_mtok": "100"}
    e = parsed({"mode": "transcription", "meters": meters})
    only_seconds = worst_case(e, Estimate(audio_seconds=Decimal(60)))
    assert only_seconds == Money.from_amount(1, "USD")
    both = worst_case(e, Estimate(audio_seconds=Decimal(60), audio_input_tokens=100_000))
    assert both == Money.from_amount(10, "USD")  # tokens 100k x 100 / 1e6 beats 1 minute
    cheap_tokens = worst_case(e, Estimate(audio_seconds=Decimal(60), audio_input_tokens=10))
    assert cheap_tokens == Money.from_amount(1, "USD")


def test_worst_case_audio_tokens_only_entry() -> None:
    e = parsed({"mode": "transcription", "meters": {"audio_input_per_mtok": "8"}})
    assert worst_case(e, Estimate(audio_input_tokens=1000)) == Money(8_000_000, "USD")
    with pytest.raises(UnpricedUsage):
        worst_case(e, Estimate(audio_seconds=Decimal(5)))


def test_worst_case_speech_adds_characters_and_audio_output() -> None:
    e = parsed(
        {"mode": "speech", "meters": {"per_1k_characters": "0.1", "audio_output_per_mtok": "20"}}
    )
    got = worst_case(e, Estimate(characters=1000, max_output_tokens=500))
    assert got == Money.from_amount(Decimal("0.1") + Decimal(500 * 20) / 10**6, "USD")


def test_worst_case_for_tokens_the_entry_cannot_price_is_refused() -> None:
    e = parsed({"mode": "speech", "meters": {"per_1k_characters": "1"}})
    with pytest.raises(UnpricedUsage, match="token meter"):
        worst_case(e, Estimate(input_tokens=5))


def test_worst_case_of_an_empty_estimate_is_zero() -> None:
    e = parsed({"meters": {"input_per_mtok": "1", "output_per_mtok": "1"}})
    assert worst_case(e, Estimate()) == Money(0, "USD")


# --- property: the hold always covers the real cost ----------------------------------------------

PRICE = st.integers(0, 10**6).map(lambda n: str(Decimal(n) / 1000))
MULT = st.integers(1, 5000).map(lambda n: str(Decimal(n) / 1000))


@st.composite
def rich_entries(draw: st.DrawFn) -> PriceEntry:
    optional = ("cached_input_per_mtok", "cache_write_per_mtok", "reasoning_per_mtok",
                "audio_input_per_mtok", "cached_audio_input_per_mtok", "audio_output_per_mtok",
                "audio_input_per_minute", "per_1k_characters", "per_image")  # fmt: skip
    meters = {"input_per_mtok": draw(PRICE), "output_per_mtok": draw(PRICE)}
    for name in optional:
        if draw(st.booleans()):
            meters[name] = draw(PRICE)
    meters["audio_granularity_seconds"] = draw(st.integers(1, 30))  # type: ignore[assignment]
    extra: dict[str, object] = {"meters": meters, "price_multiplier": draw(MULT)}
    if draw(st.booleans()):
        extra["long_context"] = {
            "threshold_input_tokens": draw(st.integers(1, 5000)),
            "input_multiplier": draw(MULT),
            "output_multiplier": draw(MULT),
        }
    return parsed(extra)


@st.composite
def estimate_and_usage(draw: st.DrawFn) -> tuple[Estimate, Usage]:
    est = Estimate(
        input_tokens=draw(st.integers(0, 10_000)),
        max_output_tokens=draw(st.integers(0, 10_000)),
        audio_input_tokens=draw(st.integers(0, 10_000)),
        audio_seconds=Decimal(draw(st.integers(0, 600_000))) / 1000,
        characters=draw(st.integers(0, 10_000)),
        images=draw(st.integers(0, 20)),
    )
    inp = draw(st.integers(0, est.input_tokens))
    cached = draw(st.integers(0, inp))
    write = draw(st.integers(0, inp - cached))
    text_out = draw(st.integers(0, est.max_output_tokens))
    audio_out = draw(st.integers(0, est.max_output_tokens - text_out))
    # Minute and token meters are alternatives for one clip, as the adapter reports it.
    by_seconds = draw(st.booleans())
    clip = Decimal(draw(st.integers(0, int(est.audio_seconds * 1000)))) / 1000
    seconds = clip if by_seconds else Decimal(0)
    usage = Usage(
        input_tokens=inp,
        cached_input_tokens=cached,
        cache_write_tokens=write,
        output_tokens=text_out,
        reasoning_tokens=draw(st.integers(0, text_out)),
        audio_output_tokens=audio_out,
        audio_input_tokens=0 if by_seconds else draw(st.integers(0, est.audio_input_tokens)),
        cached_audio_input_tokens=0,
        audio_seconds=seconds,
        characters=draw(st.integers(0, est.characters)),
        images=draw(st.integers(0, est.images)),
    )
    return est, usage


@settings(max_examples=300, deadline=None)
@given(entry=rich_entries(), pair=estimate_and_usage())
def test_worst_case_is_never_below_the_actual_cost_of_usage_within_the_estimate(
    entry: PriceEntry, pair: tuple[Estimate, Usage]
) -> None:
    est, usage = pair
    try:
        actual = actual_cost(entry, usage)
        worst = worst_case(entry, est)
    except UnpricedUsage:
        return  # an unmetered quantity is refused on both sides, never silently free
    assert worst >= actual
