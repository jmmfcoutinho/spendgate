"""The bundled Azure OpenAI entries that scripts/azure_prices.py generates (#106).

One id per deployment type (`azure_openai/global.<model>`, `datazone.<model>`, `regional.<model>`),
Region-scoped where Azure's Retail Prices API lists different prices per Region. These tests hold
the bundled block to that structure and to the recorded API items in tests/data/azure_retail/.
"""
# ruff: noqa: ANN401  (the script is loaded dynamically, so its types are Any here)

import importlib.util
import sys
from collections import defaultdict
from decimal import Decimal
from importlib import resources
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from spendgate.core.catalog import Catalog, ModelNotPriced, parse

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "tests" / "data" / "azure_retail" / "items.json"
NOW_MS = 1_795_651_200_000  # 2026-11-26
PREFIXES = ("global.", "datazone.", "regional.")
# The hand-written upper-bound ids, which stay as they were (region-less).
HAND_WRITTEN = {
    "gpt-6-astra", "gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol", "whisper", "gpt-transcribe",
    "gpt-live-transcribe", "gpt-4o-transcribe", "gpt-4o-mini-transcribe",
}  # fmt: skip


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("azure_prices", REPO / "scripts/azure_prices.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["azure_prices"] = module
    spec.loader.exec_module(module)
    return module


ap = load_script()
TEXT = (resources.files("spendgate.catalog") / "prices.yaml").read_text(encoding="utf-8")
DOC = yaml.safe_load(TEXT)
CATALOG: Catalog = parse([DOC], "0" * 64, ledger_currency="USD")


def generated() -> dict[str, list[dict[str, Any]]]:
    block = yaml.safe_load("models:\n" + ap.current_block(TEXT))["models"]
    found: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in block:
        found[entry["id"].removeprefix("azure_openai/")].append(entry)
    return dict(found)


GENERATED = generated()


def test_the_block_holds_only_deployment_type_ids_and_the_bare_ids_are_unchanged() -> None:
    assert GENERATED, "the generated Azure OpenAI block is missing"
    assert all(i.startswith(PREFIXES) for i in GENERATED)
    bare = [e for e in DOC["models"] if e["id"].startswith("azure_openai/")]
    bare = [e for e in bare if not e["id"].removeprefix("azure_openai/").startswith(PREFIXES)]
    assert {e["id"].removeprefix("azure_openai/") for e in bare} == HAND_WRITTEN
    assert all("regions" not in e for e in bare)
    dtype_of = {prefix: dtype for prefix, dtype, _ in ap.DEPLOYMENTS.values()}
    for model, entries in GENERATED.items():
        for e in entries:
            assert e["verified"] is True and e["source_url"] == ap.SOURCE_URL
            assert e["deployment_type"] == dtype_of[model.split(".", 1)[0]]


def prices(entry: Any) -> dict[str, str]:
    dumped = entry.meters.model_dump(exclude_none=True)
    return {k: str(v) for k, v in dumped.items() if k != "audio_granularity_seconds"}


@pytest.mark.parametrize("model", sorted(GENERATED))
def test_every_generated_entry_resolves_for_each_region_it_lists(model: str) -> None:
    entries = GENERATED[model]
    scoped = [e for e in entries if "regions" in e]
    for e in scoped:
        for region in e["regions"]:
            found = CATALOG.lookup("azure_openai", model, NOW_MS, region)
            assert found.regions is not None and region in found.regions
            assert prices(found) == e["meters"]
    if scoped:  # prices differ by Region: no Region-less entry, so an unknown Region is refused
        assert len(scoped) == len(entries)
        with pytest.raises(ModelNotPriced):
            CATALOG.lookup("azure_openai", model, NOW_MS)
        with pytest.raises(ModelNotPriced):
            CATALOG.lookup("azure_openai", model, NOW_MS, "nowhere")
    else:
        (only,) = entries
        found = CATALOG.lookup("azure_openai", model, NOW_MS, "eastus")
        assert found.regions is None and found.effective_from.isoformat() == only["effective_from"]


def test_known_meters_match_the_recorded_retail_prices_items() -> None:
    checked = 0
    for item in ap.load_items(DATA.read_bytes()):
        if item["productName"] not in ap._PRODUCTS:
            continue
        meter = ap.classify(item["meterName"])
        if isinstance(meter, ap.Skip):
            continue
        model = f"{dict(ap.DEPLOYMENTS)[meter.dtype][0]}.{meter.model}"
        entry = CATALOG.lookup("azure_openai", model, NOW_MS, item["armRegionName"])
        expected = ap.per_unit(meter.meter, item["unitOfMeasure"], item["retailPrice"], "x")
        price = getattr(entry.meters, meter.meter)
        if meter.tier == "long":
            assert entry.long_context is not None
            in_side = meter.meter in ap.INPUT_SIDE
            lc = entry.long_context
            price *= lc.input_multiplier if in_side else lc.output_multiplier
        assert price == expected, (model, item["armRegionName"], meter)
        checked += 1
    assert checked >= 50
    sweden = CATALOG.lookup("azure_openai", "datazone.gpt-6-luna", NOW_MS, "swedencentral")
    assert sweden.meters.input_per_mtok == Decimal("0.12")  # "6-luna ShortCo Inp Std DZ", 0.12/1M
    nano = CATALOG.lookup("azure_openai", "global.gpt-4.1-nano", NOW_MS, "eastus2")
    assert nano.meters.cached_input_per_mtok == Decimal("0.025")  # 0.000025 USD per 1K


def test_no_zone_or_regional_meter_is_cheaper_than_the_global_one() -> None:
    for model, entries in GENERATED.items():
        prefix, name = model.split(".", 1)
        world = GENERATED.get(f"global.{name}")
        if prefix == "global" or world is None or len(world) != 1:
            continue
        for e in entries:
            for meter, value in e["meters"].items():
                if meter in world[0]["meters"]:
                    assert Decimal(value) >= Decimal(world[0]["meters"][meter]), (model, meter)


def test_long_context_uses_the_threshold_of_the_matching_openai_entry() -> None:
    openai = {
        e["id"].removeprefix("openai/"): e for e in DOC["models"] if e["id"].startswith("openai/")
    }
    compared = 0
    for model, entries in GENERATED.items():
        theirs = openai.get(model.split(".", 1)[1], {}).get("long_context")
        for e in entries:
            if "long_context" in e and theirs is not None:
                assert (
                    e["long_context"]["threshold_input_tokens"] == theirs["threshold_input_tokens"]
                )
                compared += 1
    assert compared > 0


def test_a_route_within_one_price_group_resolves_to_one_entry() -> None:
    # westeurope and swedencentral are both EU Data Zone Regions at the same price
    entry = CATALOG.lookup_route(
        "azure_openai", "datazone.gpt-6-luna", ["westeurope", "swedencentral"], NOW_MS
    )
    assert entry.meters.input_per_mtok == Decimal("0.12")
    with pytest.raises(ModelNotPriced):
        CATALOG.lookup_route(
            "azure_openai", "datazone.gpt-6-luna", ["westeurope", "eastus2"], NOW_MS
        )
