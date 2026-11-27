"""The bundled Bedrock Claude 5.x entries (issues #100, #105): one per inference profile AWS offers.

AWS's Price List (read 2026-11-26) has one global price per Claude 5.x model and a standard price,
for the geographic profiles and in-Region use, of exactly 1.1x it in every commercial Region. These
tests hold the generated catalog to that structure, so a mapping slip in the importer shows up as a
broken ratio, and to the profile ids AWS's model cards list, so a geography is neither dropped nor
invented. Every Bedrock entry must also resolve, in a Region it prices.
"""

import re
from decimal import Decimal
from importlib import resources
from typing import Any

import pytest
import yaml

from spendgate.core.catalog import parse

GEOGRAPHIC = ("us", "ca", "eu", "au", "jp", "in")
# Inference profiles on bedrock-runtime per AWS's model cards (2026-11-26); "" = bare id.
PROFILES = {
    "claude-fable-5": {"us", "global"},
    "claude-fable-5-1": {"us", "global"},
    "claude-fable-5-2": {"us", "global"},
    "claude-haiku-5-5": {"us", "ca", "eu", "au", "jp", "global", ""},  # "" kept (see the note)
    "claude-mythos-5-1": {"us", "global"},
    "claude-mythos-5-2": {"us", "global"},
    "claude-opus-5": {"us", "eu", "au", "in", "global"},
    "claude-opus-5-5": {"us", "eu", "au", "jp", "global", ""},
    "claude-sonnet-5": {"us", "eu", "au", "in", "global"},
    "claude-sonnet-5-5": {"us", "ca", "eu", "au", "global"},
}
CLAUDE_5 = re.compile(r"claude-(fable|haiku|mythos|opus|sonnet)-5(-\d+)?")
STANDARD = Decimal("1.1")  # geographic or in-Region over global, in every commercial Region
NOW_MS = 1_795_651_200_000  # 2026-11-26


def bundled() -> list[dict[str, Any]]:
    text = (resources.files("spendgate.catalog") / "prices.yaml").read_text(encoding="utf-8")
    models: list[dict[str, Any]] = yaml.safe_load(text)["models"]
    return [e for e in models if e["id"].startswith("bedrock/")]


def split(model_id: str) -> tuple[str, str]:
    """(`us`, `claude-haiku-5-5`) from `bedrock/us.anthropic.claude-haiku-5-5`; "" in-Region."""
    prefix, _, name = model_id.removeprefix("bedrock/").rpartition("anthropic.")
    return prefix.removesuffix("."), name


def claude_5() -> list[dict[str, Any]]:
    return [e for e in bundled() if CLAUDE_5.fullmatch(split(e["id"])[1])]


def test_bedrock_claude_5_profiles_are_the_ones_aws_offers_per_model() -> None:
    found: dict[str, set[str]] = {}
    for entry in claude_5():
        prefix, name = split(entry["id"])
        found.setdefault(name, set()).add(prefix)
    assert found == PROFILES


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_every_standard_bedrock_price_is_1_1x_the_global_price_meter_by_meter(name: str) -> None:
    entries = [e for e in claude_5() if split(e["id"])[1] == name]
    (world,) = [e for e in entries if split(e["id"])[0] == "global"]
    assert world["deployment_type"] == "global"
    for entry in entries:
        prefix = split(entry["id"])[0]
        if prefix == "global":
            continue
        assert entry["deployment_type"] == ("data_zone" if prefix in GEOGRAPHIC else "regional")
        assert not any(r.startswith("us-gov-") for r in entry.get("regions", [])), entry["id"]
        assert set(entry["meters"]) == set(world["meters"])
        for meter, value in entry["meters"].items():
            expected = Decimal(world["meters"][meter]) * STANDARD
            assert Decimal(value) == expected, (prefix, name, meter)
        assert entry.get("long_context") == world.get("long_context")
        assert entry["limits"] == world["limits"]


def test_only_haiku_5_5_has_the_long_context_surcharge_among_bedrock_claude_models() -> None:
    for entry in bundled():
        name = split(entry["id"])[1]
        if not name.startswith("claude-"):
            continue
        has_rule = "long_context" in entry
        assert has_rule == (name == "claude-haiku-5-5"), entry["id"]
        if has_rule:
            assert entry["long_context"] == {
                "threshold_input_tokens": 100000,
                "input_multiplier": "5",
                "output_multiplier": "5",
            }


def test_every_bedrock_entry_resolves_today_in_a_region_it_prices() -> None:
    text = (resources.files("spendgate.catalog") / "prices.yaml").read_text(encoding="utf-8")
    catalog = parse([yaml.safe_load(text)], "0" * 64, ledger_currency="USD")
    for raw in bundled():
        region = raw["regions"][0] if raw.get("regions") else None
        model = raw["id"].removeprefix("bedrock/")
        entry = catalog.lookup("bedrock", model, NOW_MS, region)
        assert entry.limits is not None
        assert entry.limits.max_output_tokens == raw["limits"]["max_output_tokens"]
        assert entry.meters.input_per_mtok == Decimal(raw["meters"]["input_per_mtok"])


def test_claude_5_limits_on_bedrock_are_the_model_cards() -> None:
    for entry in claude_5():
        assert entry["limits"] == {
            "max_output_tokens": 128000,
            "context_window": 1000000,
            "max_image_tokens": 6000,
        }, entry["id"]
