"""Catalog parsing, merging, dated lookup and the fail-closed rules (T-MON-07, T-MON-08, T-MON-09,
T-CAT-05)."""

import copy
import hashlib
import time
from datetime import UTC, datetime
from decimal import Decimal
from importlib import resources

import pytest
import yaml

from spendgate.core.catalog import (
    PROVIDERS,
    Catalog,
    CatalogError,
    ModelNotPriced,
    parse,
)
from spendgate.core.cost import Usage, actual_cost
from spendgate.core.errors import ErrorCode
from spendgate.core.estimate import DEFAULT_IMAGE_TOKENS, image_tokens
from spendgate.core.money import FxSnapshot, MissingFxRate, Money

SHA = "ab" * 32


def ms(day: str) -> int:
    return int(datetime.fromisoformat(f"{day}T12:00:00+00:00").timestamp() * 1000)


def entry(model: str = "openai/example", **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": model,
        "mode": "chat",
        "currency": "USD",
        "effective_from": "2026-01-01",
        "source_url": "https://example.com/pricing",
        "verified": False,
        "meters": {"input_per_mtok": "2", "output_per_mtok": "8"},
        "limits": {"max_output_tokens": 1000},
    }
    return base | over


def doc(*models: dict[str, object], **extra: object) -> dict[str, object]:
    return {"catalog_version": 1, "models": list(models) or [entry()]} | extra


def build(
    *docs: dict[str, object], ledger: str = "USD", fx: tuple[FxSnapshot, ...] = ()
) -> Catalog:
    return parse(docs, SHA, ledger_currency=ledger, fx=fx)


# --- the bundled catalog ------------------------------------------------------------------------


def bundled_text() -> str:
    return (resources.files("spendgate.catalog") / "prices.yaml").read_text(encoding="utf-8")


def test_bundled_prices_parse_in_under_50_ms() -> None:
    text = bundled_text()
    loaded = yaml.safe_load(text)  # pure-Python loader: the slow part, so it is inside the budget
    sha = hashlib.sha256(text.encode()).hexdigest()
    started = time.perf_counter()
    catalog = parse([loaded], sha, ledger_currency="USD")
    elapsed = time.perf_counter() - started
    assert elapsed < 0.05
    assert len(catalog.entries()) > 20
    assert catalog.sha256 == sha


def test_bundled_catalog_loads_and_parses_within_50_ms_end_to_end() -> None:
    text = bundled_text()
    # Best of 5: measures our code, not a busy CI runner's scheduling noise.
    timings = []
    for _ in range(5):
        started = time.perf_counter()
        # The C loader, as the config loader uses it (the pure-Python one alone is ~40 ms on CI).
        doc = yaml.load(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))  # noqa: S506
        parse([doc], SHA, ledger_currency="USD")
        timings.append(time.perf_counter() - started)
    assert min(timings) < 0.05


def test_bundled_prices_cover_only_known_providers_and_every_chat_entry_has_a_ceiling() -> None:
    catalog = parse([yaml.safe_load(bundled_text())], SHA, ledger_currency="USD")
    for e in catalog.entries():
        assert e.provider in PROVIDERS
        if e.mode in ("chat", "responses", "messages"):
            assert e.limits is not None and e.limits.max_output_tokens


# The most one image can bill, per provider, from each provider's vision docs (read 2026-11-22).
# A bundled entry may lower its image ceiling below the estimator default, never below this.
IMAGE_TOKEN_MINIMUM = {
    "openai": 36_000,  # developers.openai.com/api/docs/guides/images-vision: gpt-6-astra original
    "azure_openai": 36_000,  # the same OpenAI models
    "anthropic": 4_784,  # platform.claude.com/docs/en/build-with-claude/vision: high-res tier
    "bedrock": 4_784,  # the same Claude models on Bedrock
    "google": 1_120,  # aistudio.google.com/docs/media-resolution: Gemini 4 and 3 default
}
LARGEST_DOCUMENTED_IMAGE_TOKENS = 48_169  # OpenAI vision guide: gpt-4o-mini, high detail


def test_m14_every_bundled_entry_holds_at_least_the_documented_image_cost() -> None:
    catalog = parse([yaml.safe_load(bundled_text())], SHA, ledger_currency="USD")
    for e in catalog.entries():
        if e.mode in ("chat", "responses", "messages"):
            assert image_tokens(e) >= IMAGE_TOKEN_MINIMUM[e.provider], e.id
    assert DEFAULT_IMAGE_TOKENS >= LARGEST_DOCUMENTED_IMAGE_TOKENS
    assert max(IMAGE_TOKEN_MINIMUM.values()) <= DEFAULT_IMAGE_TOKENS


def test_bundled_free_statuses_follow_the_seed_blocks() -> None:
    catalog = parse([yaml.safe_load(bundled_text())], SHA, ledger_currency="USD")
    assert catalog.free_statuses("anthropic") == {429, 529}
    assert catalog.free_statuses("openai") == {429}


# --- adding a model by YAML only ----------------------------------------------------------------

EXTRA_MODEL = """
catalog_version: 1
models:
  - id: openai/brand-new-model
    mode: chat
    currency: USD
    effective_from: "2026-11-13"
    source_url: https://example.com/p
    verified: false
    meters: {input_per_mtok: "3", output_per_mtok: "9"}
    limits: {max_output_tokens: 4096}
"""


def test_a_model_added_through_a_yaml_string_alone_is_priced() -> None:
    catalog = build(yaml.safe_load(EXTRA_MODEL))
    found = catalog.lookup("openai", "brand-new-model", ms("2026-11-14"))
    # (1000 * 3 + 500 * 9) / 1e6 = 0.0075 USD
    assert actual_cost(found, Usage(input_tokens=1000, output_tokens=500)) == Money(
        7_500_000, "USD"
    )


def test_override_replaces_by_id_and_date_and_adds_new_models() -> None:
    bundled = doc(entry("openai/a"), entry("openai/b"))
    override = doc(
        entry("openai/a", meters={"input_per_mtok": "1", "output_per_mtok": "2"}),
        entry("anthropic/c"),
    )
    catalog = build(bundled, override)
    assert catalog.lookup("openai", "a", ms("2026-02-01")).meters.input_per_mtok == 1
    assert catalog.lookup("openai", "b", ms("2026-02-01")).meters.input_per_mtok == 2
    assert catalog.lookup("anthropic", "c", ms("2026-02-01")).id == "anthropic/c"


def test_override_provider_block_replaces_the_bundled_one() -> None:
    bundled = doc(providers={"anthropic": {"free_statuses": [429, 529]}})
    override = doc(providers={"anthropic": {"free_statuses": [429]}})
    assert build(bundled).free_statuses("anthropic") == {429, 529}
    assert build(bundled, override).free_statuses("anthropic") == {429}
    assert build(doc(providers={"openai": {"free_statuses": []}})).free_statuses("openai") == set()


def test_an_empty_provider_block_keeps_the_provider_default() -> None:
    catalog = build(doc(providers={"anthropic": {}, "openai": {}}))
    assert catalog.free_statuses("anthropic") == {429, 529}
    assert catalog.free_statuses("openai") == {429}


def test_free_statuses_must_not_repeat() -> None:
    with pytest.raises(CatalogError, match="repeat"):
        build(doc(providers={"openai": {"free_statuses": [429, 429]}}))


def test_provider_defaults_apply_without_a_block() -> None:
    catalog = build(doc())
    assert catalog.free_statuses("anthropic") == {429, 529}
    assert catalog.free_statuses("bedrock") == {429}


# --- dated lookup -------------------------------------------------------------------------------


def two_versions() -> Catalog:
    old = entry(effective_from="2026-01-01", meters={"input_per_mtok": "2", "output_per_mtok": "8"})
    new = entry(effective_from="2026-09-01", meters={"input_per_mtok": "1", "output_per_mtok": "4"})
    return build(doc(new, old))


def test_newest_entry_in_force_wins_and_older_stays_for_history() -> None:
    catalog = two_versions()
    assert catalog.lookup("openai", "example", ms("2026-08-31")).meters.input_per_mtok == 2
    assert catalog.lookup("openai", "example", ms("2026-09-01")).meters.input_per_mtok == 1
    assert catalog.lookup("openai", "example", ms("2027-01-01")).price_id == (
        "openai/example@2026-09-01"
    )


def test_lookup_uses_the_utc_date() -> None:
    catalog = two_versions()
    last_ms_of_august = int(
        datetime(2026, 8, 31, 23, 59, 59, 999000, tzinfo=UTC).timestamp() * 1000
    )
    assert catalog.lookup("openai", "example", last_ms_of_august).meters.input_per_mtok == 2


def test_not_yet_effective_entry_is_not_priced() -> None:
    with pytest.raises(ModelNotPriced) as caught:
        two_versions().lookup("openai", "example", ms("2025-12-31"))
    assert caught.value.reason == "no price entry in force"


def test_unpriced_model_raises_and_maps_to_the_protocol_refusal() -> None:
    with pytest.raises(ModelNotPriced) as caught:
        build(doc()).lookup("openai", "nope", ms("2026-02-01"))
    refusal = caught.value.refusal()
    assert refusal.code is ErrorCode.MODEL_NOT_PRICED
    assert refusal.param == "model"
    assert "openai/nope" in refusal.message


def test_same_model_name_under_another_provider_is_not_priced() -> None:
    with pytest.raises(ModelNotPriced):
        build(doc()).lookup("anthropic", "example", ms("2026-02-01"))


def test_expired_winner_is_refused_without_falling_back_to_an_older_entry() -> None:
    old = entry(effective_from="2026-01-01")
    retired = entry(effective_from="2026-06-01", deprecation_date="2026-07-01")
    catalog = build(doc(old, retired))
    assert catalog.lookup("openai", "example", ms("2026-06-30")).effective_from.month == 6
    with pytest.raises(ModelNotPriced) as caught:
        catalog.lookup("openai", "example", ms("2026-07-01"))  # first refused day
    assert "deprecation" in caught.value.reason


def test_duplicate_id_and_date_in_one_file_is_a_load_error() -> None:
    with pytest.raises(CatalogError, match="duplicate openai/example@2026-01-01"):
        build(doc(entry(), entry()))


def test_catalog_keeps_its_sha_for_the_call_row() -> None:
    assert build(doc()).sha256 == SHA


# --- fail closed --------------------------------------------------------------------------------


# T-MON-08 (at start: a chat entry with no output ceiling stops the catalog from loading)
def test_chat_entry_without_max_output_tokens_is_invalid() -> None:
    for mode in ("chat", "responses", "messages"):
        no_limits = entry(mode=mode)
        del no_limits["limits"]
        with pytest.raises(CatalogError, match="max_output_tokens"):
            build(doc(no_limits))
        with pytest.raises(CatalogError, match="max_output_tokens"):
            build(doc(entry(mode=mode, limits={"context_window": 10})))


def test_embedding_needs_no_output_ceiling() -> None:
    e = entry("openai/emb", mode="embedding", meters={"input_per_mtok": "0.02"})
    del e["limits"]
    assert build(doc(e)).lookup("openai", "emb", ms("2026-02-01")).mode == "embedding"


def test_mode_meter_rules() -> None:
    bad = {
        "chat": {"input_per_mtok": "1"},
        "embedding": {"output_per_mtok": "1"},
        "transcription": {"input_per_mtok": "1"},
        "speech": {"input_per_mtok": "1"},
    }
    for mode, meters in bad.items():
        with pytest.raises(CatalogError):
            build(doc(entry(mode=mode, meters=meters)))


def test_missing_fx_for_a_foreign_currency_is_refused_naming_the_rate() -> None:
    with pytest.raises(MissingFxRate, match="USD to EUR"):
        build(doc(), ledger="EUR")


def test_fx_snapshot_covers_the_currency() -> None:
    fx = FxSnapshot.of("USD", "EUR", "0.92", "2026-11-13", "test")
    catalog = build(doc(), ledger="EUR", fx=(fx,))
    assert catalog.in_ledger(Money(1_000_000_000, "USD")) == Money(920_000_000, "EUR")


def test_fx_for_the_wrong_direction_does_not_count() -> None:
    fx = FxSnapshot.of("EUR", "USD", "1.08", "2026-11-13", "test")
    with pytest.raises(MissingFxRate):
        build(doc(), ledger="EUR", fx=(fx,))


def test_same_currency_needs_no_fx() -> None:
    assert build(doc(), ledger="USD").in_ledger(Money(5, "USD")) == Money(5, "USD")


def test_foreign_currency_in_an_override_needs_fx_too() -> None:
    with pytest.raises(MissingFxRate, match="EUR to USD"):
        build(doc(), doc(entry("openai/eu", currency="EUR")))


@pytest.mark.parametrize("statuses", [[500], [429, 503], [200], [429, 529, 502]])
def test_free_statuses_outside_429_and_529_are_rejected(statuses: list[int]) -> None:
    with pytest.raises(CatalogError, match="429 and 529"):
        build(doc(providers={"openai": {"free_statuses": statuses}}))


def test_free_statuses_must_be_integers() -> None:
    for bad in (["429"], [True], 429):
        with pytest.raises(CatalogError):
            build(doc(providers={"openai": {"free_statuses": bad}}))


# T-MON-09
def test_charge_zero_and_any_other_fail_open_knob_is_rejected() -> None:
    for knob in ({"charge_zero": True}, {"on_missing_usage": "charge_zero"}):
        with pytest.raises(CatalogError, match="Extra inputs"):
            build(doc(providers={"openai": knob}))
        with pytest.raises(CatalogError, match="Extra inputs"):
            build(doc(entry(**knob)))
        with pytest.raises(CatalogError, match="Extra inputs"):
            build(doc(**knob))


def test_provider_slugs_must_be_the_seven_module_names() -> None:
    assert PROVIDERS == (
        "openai", "azure_openai", "anthropic", "bedrock", "google", "azure_speech", "elevenlabs",
    )  # fmt: skip
    for slug in PROVIDERS:
        assert build(doc(entry(f"{slug}/m"))).lookup(slug, "m", ms("2026-02-01"))
    with pytest.raises(CatalogError, match="not one of"):
        build(doc(entry("vertex/gemini")))
    with pytest.raises(CatalogError, match="not one of"):
        build(doc(entry("mistral/m")))
    with pytest.raises(
        CatalogError, match=r"models\.0\.id"
    ):  # hyphenated slugs fail the id pattern
        build(doc(entry("azure-openai/m")))
    with pytest.raises(CatalogError, match="unknown provider"):
        build(doc(providers={"vertex": {"free_statuses": [429]}}))


def test_floats_are_rejected_for_prices_and_multipliers() -> None:
    with pytest.raises(CatalogError, match="decimal string"):
        build(doc(entry(meters={"input_per_mtok": 2.0, "output_per_mtok": "8"})))
    with pytest.raises(CatalogError, match="decimal string"):
        build(doc(entry(price_multiplier=1.2)))


def test_a_zero_multiplier_would_make_calls_free_and_is_rejected() -> None:
    with pytest.raises(CatalogError):
        build(doc(entry(price_multiplier="0")))
    with pytest.raises(CatalogError):
        build(doc(entry(long_context={
            "threshold_input_tokens": 10, "input_multiplier": "0", "output_multiplier": "1",
        })))  # fmt: skip


# T-CAT-01
def test_unknown_catalog_version_and_empty_models_are_rejected() -> None:
    with pytest.raises(CatalogError):
        build({"catalog_version": 2, "models": [entry()]})
    with pytest.raises(CatalogError):
        build({"catalog_version": 1, "models": []})
    with pytest.raises(CatalogError):
        build(doc(entry(meters={})))


def test_errors_name_the_document_and_field_but_not_the_value() -> None:
    bad = entry(source_url="http://secret.example/x")
    with pytest.raises(CatalogError) as caught:
        build(doc(), doc(bad))
    text = str(caught.value)
    assert "document 1" in text and "models.0.source_url" in text
    assert "secret.example" not in text


def test_bad_sha_and_no_documents_are_rejected() -> None:
    with pytest.raises(CatalogError, match="sha256"):
        parse([doc()], "nothex", ledger_currency="USD")
    with pytest.raises(CatalogError, match="no catalog"):
        parse([], SHA, ledger_currency="USD")


def test_input_documents_are_not_mutated() -> None:
    source = doc(entry(), providers={"openai": {"free_statuses": [429]}})
    before = copy.deepcopy(source)
    build(source)
    assert source == before


def test_decimals_are_exact_not_floats() -> None:
    e = build(doc(entry(meters={"input_per_mtok": "0.1", "output_per_mtok": "0.30000000001"})))
    found = e.lookup("openai", "example", ms("2026-02-01"))
    assert found.meters.output_per_mtok == Decimal("0.30000000001")
