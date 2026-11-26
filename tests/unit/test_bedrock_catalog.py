"""The bundled Bedrock Claude entries (issue #100): one per inference profile AWS offers.

AWS's Price List (read 2026-11-26) has one global price per model and a standard price, for the
geographic profiles and in-Region use, of exactly 1.1x it in every commercial Region. These tests
hold the catalog to that structure, so a typo in one profile's meters shows up as a broken ratio,
and to the profile ids AWS's model cards list, so a geography is neither dropped nor invented.
"""

from decimal import Decimal
from importlib import resources
from typing import Any

import pytest
import yaml

from spendgate.core.catalog import parse

GEOGRAPHIC = ("us", "ca", "eu", "au", "jp")
# Inference profiles on bedrock-runtime per AWS's model cards (2026-11-26); "" = bare in-Region id.
PROFILES = {
    "claude-haiku-5-5": {"us", "ca", "eu", "au", "jp", "global", ""},
    "claude-sonnet-5-5": {"us", "ca", "eu", "au", "global"},
    "claude-opus-5-5": {"us", "eu", "au", "jp", "global", ""},
    "claude-fable-5-2": {"us", "global"},
}
STANDARD = Decimal("1.1")  # geographic or in-Region over global, in every commercial Region
NOW_MS = 1_795_651_200_000  # 2026-11-26


def bedrock_entries() -> dict[str, dict[str, Any]]:
    text = (resources.files("spendgate.catalog") / "prices.yaml").read_text(encoding="utf-8")
    entries = yaml.safe_load(text)["models"]
    return {e["id"].removeprefix("bedrock/"): e for e in entries if e["id"].startswith("bedrock/")}


def split(model_id: str) -> tuple[str, str]:
    """(`us`, `claude-haiku-5-5`) from `us.anthropic.claude-haiku-5-5`; "" in-Region."""
    prefix, _, name = model_id.rpartition("anthropic.")
    return prefix.removesuffix("."), name


def test_bedrock_claude_profiles_are_the_ones_aws_offers_per_model() -> None:
    found: dict[str, set[str]] = {}
    for model_id in bedrock_entries():
        prefix, name = split(model_id)
        found.setdefault(name, set()).add(prefix)
    assert found == PROFILES


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_every_standard_bedrock_price_is_1_1x_the_global_price_meter_by_meter(name: str) -> None:
    entries = bedrock_entries()
    world = entries[f"global.anthropic.{name}"]
    assert world["deployment_type"] == "global"
    for prefix in PROFILES[name] - {"global"}:
        entry = entries[f"{prefix}.anthropic.{name}" if prefix else f"anthropic.{name}"]
        assert entry["deployment_type"] == ("data_zone" if prefix in GEOGRAPHIC else "regional")
        assert set(entry["meters"]) == set(world["meters"])
        for meter, value in entry["meters"].items():
            expected = Decimal(world["meters"][meter]) * STANDARD
            assert Decimal(value) == expected, (prefix, name, meter)
        assert entry.get("long_context") == world.get("long_context")
        assert entry["limits"] == world["limits"]


def test_only_haiku_5_5_has_the_long_context_surcharge_on_bedrock() -> None:
    for model_id, entry in bedrock_entries().items():
        has_rule = "long_context" in entry
        assert has_rule == (split(model_id)[1] == "claude-haiku-5-5"), model_id
        if has_rule:
            assert entry["long_context"] == {
                "threshold_input_tokens": 100000,
                "input_multiplier": "5",
                "output_multiplier": "5",
            }


def test_every_bedrock_profile_resolves_today_through_the_bundled_catalog() -> None:
    text = (resources.files("spendgate.catalog") / "prices.yaml").read_text(encoding="utf-8")
    catalog = parse([yaml.safe_load(text)], "0" * 64, ledger_currency="USD")
    for model_id in bedrock_entries():
        entry = catalog.lookup("bedrock", model_id, NOW_MS)
        assert entry.limits is not None and entry.limits.max_output_tokens == 128000
        assert entry.meters.input_per_mtok is not None and entry.meters.output_per_mtok is not None
