"""scripts/bedrock_prices.py: the Bedrock importer, on a recorded subset of the AWS Price List.

No network: tests/data/aws_pricelist/ holds a trimmed copy of both offers (a few products and
their on-demand terms, as AWS published them) and a small model table; downloads use a fake.
"""
# ruff: noqa: ANN401  (the script is loaded dynamically, so its types are Any here)

import importlib.util
import json
import shutil
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from spendgate.core.catalog import ModelNotPriced, parse

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "tests" / "data" / "aws_pricelist"
FM, AB = "AmazonBedrockFoundationModels", "AmazonBedrock"
TODAY = "2026-11-26"
P = "USE1-MP:USE1_"  # a Region prefix of the FoundationModels usage types
S, G = "standard", "global"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "bedrock_prices", REPO / "scripts/bedrock_prices.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["bedrock_prices"] = module
    spec.loader.exec_module(module)
    return module


bp = load_script()


def table() -> Any:
    return bp.load_table(DATA / "models.yaml")


def entries(directory: Path = DATA) -> dict[str, list[dict[str, Any]]]:
    block, _ = bp.generate(directory, table(), TODAY)
    doc = yaml.safe_load("models:\n" + block)
    out: dict[str, list[dict[str, Any]]] = {}
    for e in doc["models"]:
        out.setdefault(e["id"], []).append(e)
    return out


def meters(entry: dict[str, Any]) -> dict[str, Decimal]:
    return {k: Decimal(v) for k, v in entry["meters"].items()}


def copy_data(tmp_path: Path) -> Path:
    shutil.copytree(DATA, tmp_path / "pl")
    return tmp_path / "pl"


def add_product(directory: Path, offer: str, region: str, attrs: dict[str, str], usd: str) -> None:
    """One more product with one on-demand price in a copied Region file."""
    path = directory / offer / f"{region}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    sku = "TESTSKU" + str(len(data["products"]))
    attrs = {"regionCode": region, "feature": "On-demand Inference"} | attrs
    data["products"][sku] = {"sku": sku, "attributes": attrs}
    unit = "1M tokens" if offer == FM else "1K tokens"
    data["terms"]["OnDemand"][sku] = {
        f"{sku}.T": {
            "effectiveDate": "2026-11-01T00:00:00Z",
            "priceDimensions": {f"{sku}.T.D": {"unit": unit, "pricePerUnit": {"USD": usd}}},
        }
    }
    path.write_text(json.dumps(data), encoding="utf-8")


# --- usage types -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offer", "usagetype", "meter", "scope", "long"),
    [
        (FM, P + "input_tokens_standard-Units", "input", S, False),
        (FM, P + "output_tokens_global_standard-Units", "output", G, False),
        (FM, P + "cache_read_tokens_long_ctx_standard-Units", "cached_input", S, True),
        (FM, P + "cached_input_tokens_standard-Units", "cached_input", S, False),
        (FM, P + "cache_write_tokens_standard-Units", "cache_write_5m", S, False),
        (FM, P + "cache_write_tokens_30m_global_standard-Units", "cache_write_30m", G, False),
        (FM, P + "cache_write_tokens_1h_long_ctx_global_standard-Units", "cache_write_1h", G, True),
        (FM, P + "InputTokenCount-Units", "input", S, False),
        (FM, P + "OutputTokenCount_Global-Units", "output", G, False),
        (FM, P + "CacheReadInputTokenCount-Units", "cached_input", S, False),
        (FM, P + "CacheWrite1hInputTokenCount_Global-Units", "cache_write_1h", G, False),
        (AB, "USE1-Llama3-3-70B-input-tokens", "input", S, False),
        (AB, "USE1-Nova2.0Pro-text-output-tokens", "output", S, False),
        (AB, "EUW2-NovaPro-cache-read-input-token-count", "cached_input", S, False),
        (AB, "APE2-Nova2.0Lite-input-tokens-cross-region-global", "input", G, False),
        (AB, "USE1-zai.glm-4.7-output-tokens", "output", S, False),
    ],
)  # fmt: skip
def test_every_usage_type_family_maps_to_its_meter(
    offer: str, usagetype: str, meter: str, scope: str, long: bool
) -> None:
    assert bp.classify(offer, usagetype) == bp.Meter(meter, scope, long)


@pytest.mark.parametrize(
    ("offer", "usagetype", "reason"),
    [
        (FM, P + "input_tokens_ultrafast-Units", "Ultrafast"),
        (FM, P + "InputTokenCount_LatencyOptimized-Units", "latency-optimized"),
        (AB, "USE1-NovaPro-input-tokens-flex", "Flex"),
        (AB, "APE2-Nova2.0Lite-input-tokens-cross-region-global-batch", "batch"),
        (AB, "APE2-NovaPro-cache-write-input-token-count-priority-cross-region-global", "Prio"),
        (AB, "USE1-deepseek.v3.2-mantle-input-tokens-standard", "bedrock-mantle"),
        (AB, "USE1-Nova2.0Omni-input-audio-token-count", "audio"),
        (AB, "USE1-Nova2.0Lite-nova-grounding", "grounding"),
        (AB, "USE1-Llama3-3-70B-input-tokens-custom-model", "custom"),
    ],
)  # fmt: skip
def test_tiers_and_endpoints_the_adapter_never_uses_are_skipped_with_a_reason(
    offer: str, usagetype: str, reason: str
) -> None:
    what = bp.classify(offer, usagetype)
    assert isinstance(what, bp.Skip) and reason in what.reason


@pytest.mark.parametrize(
    ("offer", "usagetype"),
    [
        (FM, P + "input_tokens_premium-Units"),
        (FM, P + "ReasoningTokenCount-Units"),
        (AB, "USE1-NovaPro-reasoning-tokens"),
    ],
)
def test_an_unknown_usage_type_is_not_guessed(offer: str, usagetype: str) -> None:
    assert bp.classify(offer, usagetype) is None


# --- building entries from the recording ---------------------------------------------------


def test_each_route_gets_its_own_id_and_deployment_type() -> None:
    got = entries()
    assert sorted(got) == [
        "bedrock/amazon.nova-pro-v1:0",
        "bedrock/anthropic.claude-haiku-5-5",
        "bedrock/deepseek.v3.2",
        "bedrock/eu.anthropic.claude-haiku-5-5",
        "bedrock/global.anthropic.claude-haiku-5-5",
        "bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "bedrock/us.anthropic.claude-haiku-5-5",
        "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    ]
    kinds = {i: {e["deployment_type"] for e in es} for i, es in got.items()}
    assert kinds["bedrock/global.anthropic.claude-haiku-5-5"] == {"global"}
    assert kinds["bedrock/eu.anthropic.claude-haiku-5-5"] == {"data_zone"}
    assert kinds["bedrock/deepseek.v3.2"] == {"regional"}
    for es in got.values():
        assert all(e["verified"] is True and e["checked_on"] == TODAY for e in es)
        assert all(e["source_url"] == "https://aws.amazon.com/bedrock/pricing/" for e in es)
        assert all(e["effective_from"] == "2026-11-01" for e in es)  # the terms' effectiveDate


def test_geographic_and_global_profiles_carry_the_standard_and_global_prices() -> None:
    got = entries()
    (world,) = got["bedrock/global.anthropic.claude-haiku-5-5"]
    (eu,) = got["bedrock/eu.anthropic.claude-haiku-5-5"]
    assert "regions" not in world and "regions" not in eu  # one price in all source Regions
    assert meters(world) == {
        "input_per_mtok": Decimal("0.1"),
        "cached_input_per_mtok": Decimal("0.01"),
        "cache_write_per_mtok": Decimal("0.2"),  # the 1h price, not the 5m one (0.125)
        "output_per_mtok": Decimal("0.5"),
    }
    assert {k: v / Decimal("1.1") for k, v in meters(eu).items()} == meters(world)


def test_older_camel_case_usage_types_price_the_same_routes() -> None:
    got = entries()
    (us,) = got["bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0"]
    assert meters(us) == {
        "input_per_mtok": Decimal("3.3"),
        "cached_input_per_mtok": Decimal("0.33"),
        "cache_write_per_mtok": Decimal("6.6"),
        "output_per_mtok": Decimal("16.5"),
    }
    assert us["deprecation_date"] == "2027-04-08"


def test_long_context_rows_become_the_entry_rule_with_the_documented_threshold() -> None:
    for entry_id in (
        "bedrock/us.anthropic.claude-haiku-5-5",
        "bedrock/global.anthropic.claude-haiku-5-5",
    ):
        (entry,) = entries()[entry_id]
        assert entry["long_context"] == {
            "threshold_input_tokens": 100000,
            "input_multiplier": "5",
            "output_multiplier": "5",
        }
    (sonnet,) = entries()["bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0"]
    assert "long_context" not in sonnet  # no long-context rows, no rule


def test_long_context_rows_without_a_documented_threshold_are_refused(tmp_path: Path) -> None:
    models = yaml.safe_load((DATA / "models.yaml").read_text(encoding="utf-8"))
    del models["models"][0]["long_context"]
    (tmp_path / "m.yaml").write_text(yaml.safe_dump(models), encoding="utf-8")
    with pytest.raises(bp.InputError, match="no documented threshold"):
        bp.generate(DATA, bp.load_table(tmp_path / "m.yaml"), TODAY)


def test_prices_that_differ_by_region_get_one_region_scoped_entry_each() -> None:
    deepseek = entries()["bedrock/deepseek.v3.2"]
    by_regions = {tuple(e["regions"]): meters(e) for e in deepseek}
    assert by_regions == {
        ("eu-west-2",): {"input_per_mtok": Decimal("0.961"), "output_per_mtok": Decimal("2.8675")},
        ("us-east-1",): {"input_per_mtok": Decimal("0.62"), "output_per_mtok": Decimal("1.85")},
    }
    # eu-west-1 is in the table but has no price: no entry covers it, the note says so.
    assert all("eu-west-1: refused there" in e["note"] for e in deepseek)


def test_the_generated_block_loads_and_resolves_per_region() -> None:
    block, _ = bp.generate(DATA, table(), TODAY)
    doc = {"catalog_version": 1, "models": yaml.safe_load("models:\n" + block)["models"]}
    catalog = parse([doc], "0" * 64, ledger_currency="USD")
    now = 1_795_651_200_000  # 2026-11-26
    us = catalog.lookup("bedrock", "deepseek.v3.2", now, "us-east-1")
    assert us.meters.input_per_mtok == Decimal("0.62")
    assert catalog.lookup("bedrock", "deepseek.v3.2", now, "eu-west-2").meters.input_per_mtok == (
        Decimal("0.961")
    )
    with pytest.raises(ModelNotPriced):
        catalog.lookup("bedrock", "deepseek.v3.2", now, "eu-west-1")


def test_a_free_cache_write_gets_no_meter_so_it_bills_at_the_input_price() -> None:
    nova = {tuple(e.get("regions", [])): e for e in entries()["bedrock/amazon.nova-pro-v1:0"]}
    us_east = nova[("us-east-1",)]
    assert "cache_write_per_mtok" not in us_east["meters"]
    assert "cache writes at 0" in us_east["note"]
    assert meters(us_east)["input_per_mtok"] == Decimal("0.8")  # per 1K tokens x 1000


def test_a_zero_price_on_any_other_meter_is_refused(tmp_path: Path) -> None:
    pl = copy_data(tmp_path)
    attrs = {"provider": "", "model": "Nova Pro", "usagetype": "USE1-NovaPro-output-tokens"}
    add_product(pl, AB, "us-east-1", attrs, "0")
    with pytest.raises(bp.InputError, match=r"not positive|two different"):
        bp.generate(pl, table(), TODAY)


# --- failing loudly --------------------------------------------------------------------------


def test_an_unknown_usage_type_of_a_mapped_model_is_refused(tmp_path: Path) -> None:
    pl = copy_data(tmp_path)
    attrs = {
        "provider": "DeepSeek",
        "model": "DeepSeek v3.2",
        "usagetype": "USE1-deepseek.v3.2-thinking-tokens",
    }
    add_product(pl, AB, "us-east-1", attrs, "0.001")
    with pytest.raises(bp.InputError, match="cannot map usage type"):
        bp.generate(pl, table(), TODAY)


def test_a_model_in_neither_list_of_the_table_is_refused(tmp_path: Path) -> None:
    pl = copy_data(tmp_path)
    attrs = {"provider": "Acme", "model": "Acme 1", "usagetype": "USE1-Acme1-input-tokens"}
    add_product(pl, AB, "us-east-1", attrs, "0.001")
    with pytest.raises(bp.InputError, match="unmapped model"):
        bp.generate(pl, table(), TODAY)


def test_skipped_models_and_non_model_rows_are_counted_not_priced(tmp_path: Path) -> None:
    pl = copy_data(tmp_path)
    add_product(
        pl, AB, "us-east-1", {"provider": "Cohere", "model": "Embed v4", "usagetype": "USE1-x"}, "1"
    )
    add_product(pl, AB, "us-east-1", {"usagetype": "USE1-Guardrail-TopicPolicyUnitsConsumed"}, "1")
    block, report = bp.generate(pl, table(), TODAY)
    assert "Embeddings, not chat" in report and "Guardrails" in report
    assert block == bp.generate(DATA, table(), TODAY)[0]
    add_product(pl, AB, "us-east-1", {"usagetype": "USE1-SomethingNew-Units"}, "1")
    with pytest.raises(bp.InputError, match="names no model"):
        bp.generate(pl, table(), TODAY)


def test_a_model_of_the_table_without_any_price_is_refused(tmp_path: Path) -> None:
    models = yaml.safe_load((DATA / "models.yaml").read_text(encoding="utf-8"))
    models["models"][2]["price"] = [AB, "DeepSeek", "DeepSeek v9"]
    models["skip"].append({"price": [AB, "DeepSeek", "DeepSeek v3.2"], "reason": "test"})
    (tmp_path / "m.yaml").write_text(yaml.safe_dump(models), encoding="utf-8")
    with pytest.raises(bp.InputError, match="has no price"):
        bp.generate(DATA, bp.load_table(tmp_path / "m.yaml"), TODAY)


def test_a_model_without_a_documented_output_limit_is_refused(tmp_path: Path) -> None:
    models = yaml.safe_load((DATA / "models.yaml").read_text(encoding="utf-8"))
    del models["models"][2]["limits"]["max_output_tokens"]
    (tmp_path / "m.yaml").write_text(yaml.safe_dump(models), encoding="utf-8")
    with pytest.raises(bp.InputError, match="max_output_tokens is required"):
        bp.load_table(tmp_path / "m.yaml")


# --- determinism, --write and --check ------------------------------------------------------


def test_the_output_is_deterministic_whatever_the_order_of_the_files(tmp_path: Path) -> None:
    pl = copy_data(tmp_path)
    for path in pl.glob("*/*-*-*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["products"] = dict(reversed(list(data["products"].items())))
        path.write_text(json.dumps(data), encoding="utf-8")
    assert bp.generate(pl, table(), TODAY)[0] == bp.generate(DATA, table(), TODAY)[0]


def run(*args: str, catalog: Path) -> int:
    common = ["--from-dir", str(DATA), "--models", str(DATA / "models.yaml")]
    return int(bp.main([*common, "--catalog", str(catalog), *args]))


def test_write_fills_the_block_and_check_detects_drift(tmp_path: Path) -> None:
    catalog = tmp_path / "prices.yaml"
    shutil.copy(DATA / "catalog.yaml", catalog)
    assert run("--check", catalog=catalog) == bp.EXIT_DRIFT  # the block is empty
    assert run("--write", "--today", TODAY, catalog=catalog) == 0
    text = catalog.read_text(encoding="utf-8")
    assert text.startswith((DATA / "catalog.yaml").read_text(encoding="utf-8").split(bp.BEGIN)[0])
    assert "# Hand-written comments above the block stay." in text
    parse([yaml.safe_load(text)], "0" * 64, ledger_currency="USD")
    assert run("--check", catalog=catalog) == 0
    assert run("--check", "--today", "2027-01-01", catalog=catalog) == 0  # the day is not drift
    catalog.write_text(text.replace('"0.961"', '"0.96"'), encoding="utf-8")
    assert run("--check", catalog=catalog) == bp.EXIT_DRIFT


def test_a_catalog_without_exactly_one_block_is_refused(tmp_path: Path) -> None:
    catalog = tmp_path / "prices.yaml"
    catalog.write_text("models: []\n", encoding="utf-8")
    assert run("--write", catalog=catalog) == bp.EXIT_INPUT


def test_the_bundled_model_table_loads_and_every_model_has_an_output_limit() -> None:
    loaded = bp.load_table(bp.MODELS)
    assert len(loaded.models) > 50
    for spec in loaded.models.values():
        assert spec.limits["max_output_tokens"] > 0
        assert spec.geo or spec.global_sources or spec.in_region or spec.keep_bare, spec.model_id


# --- downloading -----------------------------------------------------------------------------


def fake_get(files: dict[str, bytes]) -> Any:
    def get(url: str) -> bytes:
        return files[url]

    return get


def index(offer: str, version: str = "20261123095841") -> bytes:
    url = f"/offers/v1.0/aws/{offer}/{version}/us-east-1/index.json"
    return json.dumps({"regions": {"us-east-1": {"currentVersionUrl": url}}}).encode()


def test_download_writes_each_offer_index_and_region_file(tmp_path: Path) -> None:
    base = f"https://{bp.HOST}/offers/v1.0/aws"
    files = {}
    for offer in bp.OFFERS:
        files[f"{base}/{offer}/current/region_index.json"] = index(offer)
        files[f"{base}/{offer}/20261123095841/us-east-1/index.json"] = b'{"offerCode": "x"}'
    assert bp.download(tmp_path / "dl", fake_get(files)) == 2
    for offer in bp.OFFERS:
        assert (tmp_path / "dl" / offer / "region_index.json").read_bytes() == index(offer)
        assert (tmp_path / "dl" / offer / "us-east-1.json").exists()


def test_download_refuses_an_unexpected_file_path_and_a_used_directory(tmp_path: Path) -> None:
    base = f"https://{bp.HOST}/offers/v1.0/aws"
    evil = json.dumps({"regions": {"us-east-1": {"currentVersionUrl": "//evil.example/x"}}})
    files = {f"{base}/{bp.OFFERS[0]}/current/region_index.json": evil.encode()}
    with pytest.raises(bp.DownloadError, match="unexpected file path"):
        bp.download(tmp_path / "a", fake_get(files))
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "old.json").write_text("{}", encoding="utf-8")
    with pytest.raises(bp.InputError, match="not empty"):
        bp.download(tmp_path / "b", fake_get(files))


@pytest.mark.parametrize(
    "url",
    [
        "http://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/region_index.json",
        "https://example.com/offers/v1.0/aws/AmazonBedrock/current/region_index.json",
    ],
)
def test_fetch_refuses_anything_but_https_to_the_price_list_host(url: str) -> None:
    with pytest.raises(bp.DownloadError, match="refusing"):
        bp.fetch(url)


def test_a_redirect_is_refused() -> None:
    handler = bp._NoRedirect()
    with pytest.raises(bp.DownloadError, match="redirect"):
        handler.redirect_request(None, None, 302, "Found", None, "https://example.com/")
