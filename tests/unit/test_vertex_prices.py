"""scripts/vertex_prices.py and the Vertex AI entries it generates (issue #107, ADR-0028).

No network: the importer reads the recorded subset in tests/data/gcp_pricing/ (a trimmed snapshot
of the Cloud Billing SKUs and v1beta prices and of Google's locations page, 2026-11-26). The
bundled-catalog tests hold the generated block to the structure Google's prices have, and check
that a Vertex deployment (always with a Region) takes it while the Gemini API keeps its own price.
"""
# ruff: noqa: ANN401  (the script is loaded dynamically, so its types are Any here)

import copy
import importlib.util
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from importlib import resources
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from spendgate.config import ConfigError, check_config, load
from spendgate.core.catalog import Catalog, ModelNotPriced, parse
from tests.unit.test_config import ENV

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "tests" / "data" / "gcp_pricing"
SHA = "ab" * 32


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "vertex_prices", REPO / "scripts/vertex_prices.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["vertex_prices"] = module
    spec.loader.exec_module(module)
    return module


vp = load_script()
# The models the recorded subset covers (the locations page subset lists gemini-3-pro-image too,
# which LOCATION_SKIPS leaves out).
SUBSET = {
    m: vp.MODELS[m]
    for m in ("gemini-4-neon", "gemini-4-hydrogen", "gemini-3.1-pro-preview", "gemini-2.5-flash")
}
CATALOG_HEAD = """\
catalog_version: 1
models:
  - id: google/gemini-4-neon
    mode: chat
    currency: USD
    effective_from: "2026-11-26"
    source_url: https://ai.google.dev/gemini-api/docs/pricing
    verified: true
    meters: { input_per_mtok: "3", cached_input_per_mtok: "0.15", output_per_mtok: "15" }
    limits: { max_output_tokens: 65536 }

  # ===================== ElevenLabs =====================
"""


def source() -> Any:
    return vp.read_dir(DATA)


def ms(day: str) -> int:
    return int(datetime.fromisoformat(f"{day}T12:00:00+00:00").astimezone(UTC).timestamp()) * 1000


def generated(text: str = CATALOG_HEAD) -> tuple[str, list[dict[str, Any]]]:
    out, entries, _ = vp.generate(source(), text, SUBSET)
    return out, entries


def by_regions(entries: list[dict[str, Any]], model: str) -> dict[tuple[str, ...], Any]:
    return {tuple(e["regions"]): e for e in entries if e["id"] == f"google/{model}"}


# --- description classification ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        (
            "Gemini 4 Neon Global Text Input - Online Predictions",
            ("gemini-4-neon", "input", "global", False),
        ),
        (
            "Gemini 4 Neon Regional Image Input Caching Online",
            ("gemini-4-neon", "cached", "regional", False),
        ),
        (
            "Gemini 4 Neon Cyber Permissive Global Text Output - Online Predictions",
            ("gemini-4-neon-cyber-permissive", "output", "global", False),
        ),
        (
            "Gemini 3.5 Flash Lite Regional Text Input - Predictions",
            ("gemini-3.5-flash-lite", "input", "regional", False),
        ),
        (
            "Gemini 3.0 Pro / 3.1 Pro / 3 Pro Image - Text Input (Long) - Predictions",
            ("gemini-3.1-pro-preview", "input", "any", True),
        ),
        (
            " Gemini 2.5 Flash GA Input Text Caching (Long)",
            ("gemini-2.5-flash", "cached", "any", True),
        ),
        (
            "Gemini 2.5 Flash GA Text Output (Thinking On and Long) - Predictions",
            ("gemini-2.5-flash", "output", "any", True),
        ),
        (
            "Gemini 2.5 Pro Thinking Text Output - Predictions",
            ("gemini-2.5-pro", "output", "any", False),
        ),
        (
            "Gemini Robotics ER 2 Global Text Input Caching Online",
            ("gemini-robotics-er-2-preview", "cached", "global", False),
        ),
    ],
)
def test_standard_gemini_skus_map_to_a_model_meter_endpoint_and_tier(
    description: str, expected: tuple[str, str, str, bool]
) -> None:
    assert tuple(vp.classify(description)) == expected


@pytest.mark.parametrize(
    ("description", "reason"),
    [
        ("Gemini 4 Neon Global Text Input - Batch Predictions", "batch prediction"),
        ("Gemini 3.8 Flash Global Text Input Flex - Predictions", "Flex tier"),
        ("Gemini 3.8 Flash Regional Image Input Caching Off-Peak", "off-peak tier"),
        ("Gemini 3.1 Flash Lite Tuned Global Text Input - Predictions", "tuned-model endpoint"),
        ("Gemini 2.5 Pro Input Text Caching Storage", "context-cache storage per hour"),
        (
            "Gemini 4 Neon Global Audio Input - Online Predictions",
            "audio (the adapter admits text and images only)",
        ),
        (
            "Gemini 3.0 Pro / 3.1 Pro / 3 Pro Image - Image Output - Predictions",
            "image output (image generation)",
        ),
        ("CodeMender Gemini 4 Neon Global Text Input", "CodeMender agent"),
        (
            "Gemini 2.5 Flash Text Input - Predictions",
            "2.5 Flash preview SKUs; gemini-2.5-flash bills under '2.5 Flash GA'",
        ),
        ("Gemini 3.1 Flash Image Global Text Input - Predictions", "image generation"),
        ("Gemini 1.5 Pro Text Input - Predictions", "retired model"),
        ("Gemini 3.8 Live Text Input - Online Predictions", "Live API"),
    ],
)
def test_gemini_skus_the_adapter_never_bills_are_skipped_with_a_reason(
    description: str, reason: str
) -> None:
    assert vp.classify(description) == vp.Skip(reason)


def test_a_sku_that_is_not_about_gemini_is_ignored() -> None:
    assert vp.classify("Vertex AI: Online/Batch Prediction Nvidia Tesla P4 GPU") is None


@pytest.mark.parametrize(
    "description",
    [
        "Gemini 5 Ultra Global Text Input - Online Predictions",  # a model nobody reviewed
        "Gemini 3.8 Flash Lite Global Text Input - Predictions",  # "Lite" is not a known word
        "Gemini 4 Neon Global Text Input Surcharge - Online Predictions",
        "Gemini 4 Neon Global Input - Online Predictions",  # no modality
        "Gemini 4 Neon Global Text Output Caching",
    ],
)
def test_m10_an_unclassifiable_gemini_sku_stops_the_import(description: str) -> None:
    with pytest.raises(vp.InputError, match="Gemini SKU"):
        vp.classify(description)


def test_every_family_names_a_reviewed_model_and_no_model_is_also_skipped() -> None:
    assert set(vp.FAMILIES.values()) == set(vp.MODELS)
    assert not set(vp.MODELS) & set(vp.LOCATION_SKIPS)
    assert not set(vp.FAMILIES) & set(vp.SKIP_FAMILIES)


# --- price conversion ---------------------------------------------------------------------------


def price(units: str, nanos: int, quantity: str, **over: Any) -> dict[str, Any]:
    tier = {
        "startAmount": {"value": "0"},
        "listPrice": {"currencyCode": "USD", "units": units, "nanos": nanos},
    }
    rate = {"tiers": [tier], "unitInfo": {"unit": "count", "unitQuantity": {"value": quantity}}}
    return {"name": "skus/X/price", "currencyCode": "USD", "rate": rate} | over


@pytest.mark.parametrize(
    ("units", "nanos", "quantity", "expected"),
    [
        ("0", 82_500_000, "1000000", "0.0825"),  # v1 shows 82 nanos per token for this one
        ("4", 125_000_000, "1000000", "4.125"),
        ("16", 500_000_000, "1000000", "16.5"),
        ("0", 750, "1", "0.75"),  # 750 nanos per token
        ("0", 750, "1000", "0.00075"),  # 750 nanos per 1,000 tokens
        ("1", 0, "1", "1000000"),
    ],
)
def test_conversion_to_usd_per_million_tokens_is_exact(
    units: str, nanos: int, quantity: str, expected: str
) -> None:
    value = vp.per_mtok(price(units, nanos, quantity))
    assert isinstance(value, Decimal) and vp.dec(value) == expected


@pytest.mark.parametrize(
    "bad",
    [
        price("0", 0, "1000000"),  # a zero price is never imported
        price("0", 1, "0"),
        price("0", 1, "3"),  # not exact per million
        price("0", 10**9, "1"),
        price("0", 5, "1", currencyCode="EUR"),
    ],
)
def test_m10_a_zero_malformed_or_foreign_price_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(vp.InputError):
        vp.per_mtok(bad)


def test_a_tiered_price_is_refused() -> None:
    tiered = price("0", 5, "1")
    tiered["rate"]["tiers"].append({"startAmount": {"value": "100"}, "listPrice": {}})
    with pytest.raises(vp.InputError, match="one USD tier"):
        vp.per_mtok(tiered)


def test_the_v1_price_rounded_to_nanos_must_agree_with_the_exact_price() -> None:
    data = source()
    sku = next(s for s in data.skus if s["skuId"] == "55DF-3B05-AEE4")  # Hydrogen, 0.0825
    assert vp.v1_per_token(sku) == Decimal("0.000000082")
    assert vp.exact_price(sku, data.prices) == Decimal("0.0825")
    off = copy.deepcopy(sku)
    off["pricingInfo"][0]["pricingExpression"]["tieredRates"][0]["unitPrice"]["nanos"] = 80
    with pytest.raises(vp.InputError, match="disagree"):
        vp.exact_price(off, data.prices)


# --- Regions, grouping and tiers ----------------------------------------------------------------


def test_the_locations_page_gives_the_regions_per_model() -> None:
    where = vp.locations(source().html)
    assert where["gemini-4-neon"] == {"global", "us", "eu"}
    assert where["gemini-3.1-pro-preview"] == {"global"}
    assert {"us-central1", "europe-west4", "global"} <= where["gemini-2.5-flash"]


def test_an_unknown_availability_mark_on_the_locations_page_is_refused() -> None:
    html = source().html.replace('aria-label="Supported"', 'aria-label="Soon"', 1)
    with pytest.raises(vp.InputError, match="availability mark"):
        vp.locations(html)


def test_global_and_non_global_skus_price_their_own_regions() -> None:
    _, entries = generated()
    neon = by_regions(entries, "gemini-4-neon")
    assert set(neon) == {("global",), ("eu", "us")}
    assert neon["global",]["meters"] == {
        "input_per_mtok": "3",
        "cached_input_per_mtok": "0.15",
        "output_per_mtok": "15",
    }
    assert neon["eu", "us"]["meters"] == {
        "input_per_mtok": "3.3",
        "cached_input_per_mtok": "0.165",
        "output_per_mtok": "16.5",
    }
    assert (
        by_regions(entries, "gemini-4-hydrogen")["eu", "us"]["meters"]["cached_input_per_mtok"]
        == "0.0825"
    )
    assert entries[0]["regions"] == ["global"]  # the global entry comes first


def test_skus_without_an_endpoint_give_one_entry_for_every_listed_region() -> None:
    _, entries = generated()
    flash = by_regions(entries, "gemini-2.5-flash")
    assert len(flash) == 1
    ((regions, entry),) = flash.items()
    assert set(regions) == vp.locations(source().html)["gemini-2.5-flash"]
    # Text, image and thinking output SKUs share a meter; the long tier costs the same here.
    assert entry["meters"] == {
        "input_per_mtok": "0.3",
        "cached_input_per_mtok": "0.03",
        "output_per_mtok": "2.5",
    }
    assert "long_context" not in entry
    assert entry["deprecation_date"] == "2026-10-20"


def test_a_long_context_tier_becomes_the_documented_200k_rule() -> None:
    _, entries = generated()
    (pro,) = by_regions(entries, "gemini-3.1-pro-preview").values()
    assert pro["regions"] == ["global"]
    assert pro["long_context"] == {
        "threshold_input_tokens": 200_000,
        "input_multiplier": "2",
        "output_multiplier": "1.5",
    }


def test_a_cached_long_tier_that_is_not_the_input_multiplier_is_refused() -> None:
    rates = {
        ("any", "input", False): Decimal(2),
        ("any", "input", True): Decimal(4),
        ("any", "cached", False): Decimal("0.2"),
        ("any", "cached", True): Decimal("0.6"),
        ("any", "output", False): Decimal(12),
        ("any", "output", True): Decimal(18),
    }
    with pytest.raises(vp.InputError, match="cached long-context"):
        vp.priced("m", rates, "any")


def test_equal_global_and_non_global_prices_share_one_entry() -> None:
    rates = {
        (e, k, False): Decimal(p)
        for e in ("global", "regional")
        for k, p in (("input", "1"), ("output", "4"))
    }
    (one,) = vp.model_entries("m", vp.FLASH, rates, {"global", "us", "eu"})
    assert one["regions"] == ["eu", "global", "us"]


def test_a_region_sold_without_its_endpoint_price_is_refused() -> None:
    rates = {("global", k, False): Decimal(p) for k, p in (("input", "1"), ("output", "4"))}
    with pytest.raises(vp.InputError, match="no regional price"):
        vp.model_entries("m", vp.FLASH, rates, {"global", "us"})


def test_a_gemini_model_the_locations_page_adds_must_be_reviewed() -> None:
    data = source()
    html = data.html.replace("(gemini-3-pro-image)", "(gemini-9-new)")
    with pytest.raises(vp.InputError, match="nobody reviewed"):
        vp.build(data._replace(html=html), SUBSET)


def test_m10_an_unknown_gemini_sku_in_the_data_stops_the_import() -> None:
    data = source()
    sku = copy.deepcopy(data.skus[1])
    sku["description"] = "Gemini 5 Ultra Global Text Input - Online Predictions"
    with pytest.raises(vp.InputError, match="Gemini 5 Ultra"):
        vp.generate(data._replace(skus=[*data.skus, sku]), CATALOG_HEAD, SUBSET)


# --- the generated block, determinism and --check -----------------------------------------------


def test_the_block_is_placed_after_the_gemini_api_entries_and_loads() -> None:
    text, entries = generated()
    assert text.index("id: google/gemini-4-neon") < text.index(vp.BEGIN) < text.index("ElevenLabs")
    catalog = parse([yaml.safe_load(text)], SHA, ledger_currency="USD")
    assert len([e for e in catalog.entries() if e.regions]) == len(entries) == 6
    for e in entries:
        assert e["verified"] is True and e["source_url"] == vp.SOURCE_URL
        assert e["effective_from"] == e["checked_on"] == "2026-11-26"  # the SKUs' effectiveTime


def test_generation_is_deterministic_and_idempotent() -> None:
    once, _ = generated()
    assert generated()[0] == once
    assert generated(once)[0] == once  # regenerating over its own output changes nothing


def test_unchanged_entries_keep_their_dates_and_a_changed_price_gets_the_snapshot_day() -> None:
    once, _ = generated()
    aged = once.replace(
        'effective_from: "2026-11-26"\n    regions:', 'effective_from: "2026-11-01"\n    regions:'
    )
    again, entries = generated(aged)
    assert again == aged and {e["effective_from"] for e in entries} == {"2026-11-01"}
    data = source()
    prices = copy.deepcopy(data.prices)
    prices["BD37-A5DA-7EB1"]["rate"]["tiers"][0]["listPrice"]["units"] = "4"  # Neon global input
    skus = copy.deepcopy(data.skus)
    sku = next(s for s in skus if s["skuId"] == "BD37-A5DA-7EB1")
    sku["pricingInfo"][0]["pricingExpression"]["tieredRates"][0]["unitPrice"]["units"] = "0"
    sku["pricingInfo"][0]["pricingExpression"]["tieredRates"][0]["unitPrice"]["nanos"] = 4000
    _, changed, _ = vp.generate(data._replace(skus=skus, prices=prices), aged, SUBSET)
    neon = by_regions(changed, "gemini-4-neon")
    assert neon["global",]["effective_from"] == "2026-11-26"
    assert neon["eu", "us"]["effective_from"] == "2026-11-01"


def test_check_exits_1_on_drift_and_0_once_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(vp, "MODELS", SUBSET)
    catalog = tmp_path / "prices.yaml"
    catalog.write_text(CATALOG_HEAD, encoding="utf-8")
    args = ["--from-dir", str(DATA), "--catalog", str(catalog)]
    assert vp.main([*args, "--check"]) == 1
    assert "+  - id: google/gemini-4-neon" in capsys.readouterr().out
    assert vp.main([*args, "--write"]) == 0
    assert vp.main([*args, "--check"]) == 0
    written = catalog.read_text(encoding="utf-8")
    catalog.write_text(written.replace('"16.5"', '"1.65"'), encoding="utf-8")  # a hand edit
    assert vp.main([*args, "--check"]) == 1


def test_broken_block_markers_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vp, "MODELS", SUBSET)
    catalog = tmp_path / "prices.yaml"
    catalog.write_text(CATALOG_HEAD + vp.BEGIN + "\n", encoding="utf-8")
    assert vp.main(["--from-dir", str(DATA), "--catalog", str(catalog), "--check"]) == 2


def test_the_command_line_needs_a_source_and_an_action() -> None:
    with pytest.raises(SystemExit):
        vp.main(["--from-dir", str(DATA)])
    with pytest.raises(SystemExit):
        vp.main(["--write"])


def test_the_api_key_comes_from_the_environment_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = "fake-pricing-key-for-tests-0000000000"  # pragma: allowlist secret
    monkeypatch.setenv("GOOGLE_PRICING_API_KEY", fake)
    assert vp.api_key() == fake
    monkeypatch.delenv("GOOGLE_PRICING_API_KEY")
    monkeypatch.setenv("GOOGLE_PRICING_API_KEY_FILE", str(tmp_path / "missing"))
    with pytest.raises(vp.InputError) as info:
        vp.api_key()
    assert fake not in str(info.value)


def test_download_refuses_a_directory_that_is_not_empty(tmp_path: Path) -> None:
    (tmp_path / "x").write_text("", encoding="utf-8")
    with pytest.raises(vp.InputError, match="not empty"):
        vp.download(tmp_path)


def test_fetch_refuses_anything_but_https() -> None:
    with pytest.raises(vp.InputError, match="https"):
        vp.fetch("file:///etc/passwd", None)


# --- the Gemini API cross-check -----------------------------------------------------------------


def test_the_gemini_api_crosscheck_reports_differences_and_skips_programme_skus() -> None:
    data = source()
    rates = vp.gemini_api_rates(data.gemini_api, data.prices)
    assert rates["gemini-4-neon"] == {
        "input_per_mtok": Decimal(3),
        "cached_input_per_mtok": Decimal("0.15"),
        "output_per_mtok": Decimal(15),
    }
    assert rates["gemini-2.5-pro"] == {"input_per_mtok": Decimal("1.25")}  # long tier skipped
    catalog = yaml.safe_load(CATALOG_HEAD)
    absent = ["google/gemini-2.5-pro", "google/gemini-4-carbon"]
    assert vp.crosscheck(data, catalog, "2026-11-26") == ([], absent)
    catalog["models"][0]["meters"]["output_per_mtok"] = "14"
    found, _ = vp.crosscheck(data, catalog, "2026-11-26")
    assert found == ["google/gemini-4-neon@2026-11-26 output_per_mtok: ours 14, Gemini API SKU 15"]


def test_crosscheck_exits_1_only_on_a_price_difference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = tmp_path / "prices.yaml"
    catalog.write_text(CATALOG_HEAD, encoding="utf-8")
    args = ["--from-dir", str(DATA), "--catalog", str(catalog), "--crosscheck"]
    assert vp.main(args) == 0
    assert "google/gemini-4-carbon" in capsys.readouterr().err
    catalog.write_text(CATALOG_HEAD.replace('"0.15"', '"0.1"'), encoding="utf-8")
    assert vp.main(args) == 1


# --- the bundled catalog ------------------------------------------------------------------------

NOW_MS = ms("2026-11-27")


def bundled_text() -> str:
    return (resources.files("spendgate.catalog") / "prices.yaml").read_text(encoding="utf-8")


def vertex_entries() -> list[dict[str, Any]]:
    _, block, _ = vp.split_block(bundled_text())
    assert block is not None
    entries: list[dict[str, Any]] = yaml.safe_load("models:\n" + block)["models"]
    return entries


def bundled() -> Catalog:
    return parse([yaml.safe_load(bundled_text())], SHA, ledger_currency="USD")


def test_the_bundled_vertex_block_covers_every_reviewed_model() -> None:
    entries = vertex_entries()
    assert {e["id"].removeprefix("google/") for e in entries} == set(vp.MODELS)
    for e in entries:
        assert e["regions"] and e["verified"] is True and e["source_url"] == vp.SOURCE_URL
        assert e["limits"]["max_output_tokens"] == vp.MODELS[e["id"][7:]].max_output_tokens


def test_every_non_global_vertex_price_is_1_1x_the_global_price_meter_by_meter() -> None:
    # Pricing page, "Non-global" rows of the Gemini 3 and 4 tables: 10% over the global price.
    pairs: dict[str, dict[str, dict[str, Any]]] = {}
    for e in vertex_entries():
        pairs.setdefault(e["id"], {})["global" if e["regions"] == ["global"] else "other"] = e
    both = {m: p for m, p in pairs.items() if len(p) == 2}
    assert len(both) == 14
    for model, p in both.items():
        assert set(p["other"]["meters"]) == set(p["global"]["meters"]), model
        for name, value in p["global"]["meters"].items():
            assert Decimal(p["other"]["meters"][name]) == Decimal(value) * Decimal("1.1"), model


@pytest.mark.parametrize(
    ("region", "price_id"),
    [
        ("us", "google/gemini-4-neon@2026-11-26~us"),
        ("eu", "google/gemini-4-neon@2026-11-26~eu"),
        ("global", "google/gemini-4-neon@2026-11-26~global"),
        (None, "google/gemini-4-neon@2026-11-26"),  # the Gemini API: no Region
        ("europe-west4", "google/gemini-4-neon@2026-11-26"),  # not sold there: region-less price
    ],
)
def test_m10_a_vertex_region_takes_its_region_scoped_entry_and_the_gemini_api_its_own(
    region: str | None, price_id: str
) -> None:
    found = bundled().lookup("google", "gemini-4-neon", NOW_MS, region)
    assert found.price_id_in(region) == price_id


def test_m10_a_model_retired_on_vertex_is_refused_there_but_not_on_the_gemini_api() -> None:
    catalog = bundled()
    with pytest.raises(ModelNotPriced):
        catalog.lookup("google", "gemini-2.5-flash", NOW_MS, "us-central1")
    assert catalog.lookup("google", "gemini-2.5-flash", NOW_MS, None).verified


VERTEX_CONFIG = """\
server: {listen: 127.0.0.1:8080, key_pepper: "env:SPENDGATE_KEY_PEPPER"}
ledger: {currency: USD, backend: {kind: sqlite, path: ./l.db}}
providers:
  google: {base_url: "https://aiplatform.us.rep.googleapis.com", auth: bearer_token,
           credential: adc, region: us}
models:
  g: {catalog_id: google/gemini-4-neon, deployments: [DEPLOYMENTS]}
"""


def vertex_config(tmp_path: Path, deployments: str) -> Path:
    path = tmp_path / "spendgate.yaml"
    path.write_text(VERTEX_CONFIG.replace("DEPLOYMENTS", deployments), encoding="utf-8")
    return path


def test_a_vertex_deployment_loads_with_the_region_scoped_price(tmp_path: Path) -> None:
    path = vertex_config(tmp_path, "{provider: google}")
    report = check_config(path, env=ENV, now_ms=NOW_MS)
    assert report.ok and not report.unlisted_regions
    settings = load(path, env=ENV, now_ms=NOW_MS)
    regions = [d.region for d in settings.routes["g"].deployments]
    entry = settings.catalog.lookup_route("google", "gemini-4-neon", regions, NOW_MS)
    assert entry.price_id_in("us") == "google/gemini-4-neon@2026-11-26~us"


def test_m01_a_vertex_route_mixing_global_and_us_is_refused(tmp_path: Path) -> None:
    path = vertex_config(tmp_path, "{provider: google}, {provider: google, region: global}")
    with pytest.raises(ConfigError, match="different prices"):
        load(path, env=ENV, now_ms=NOW_MS)


def test_the_recorded_subset_is_small_and_holds_no_account_data() -> None:
    for name in vp.FILES:
        text = (DATA / name).read_text(encoding="utf-8")
        assert len(text) < 64_000 and "billingAccounts" not in text
    assert json.loads((DATA / "vertex-skus.json").read_text(encoding="utf-8"))["skus"]
