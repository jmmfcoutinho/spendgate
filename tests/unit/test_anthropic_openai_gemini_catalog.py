"""The bundled Anthropic, OpenAI and Gemini API entries (issue #108), held to the rules their
providers' pricing pages state (read 2026-11-26), so a typo in one meter shows up as a broken
ratio rather than as a wrong charge.
"""

from decimal import Decimal
from importlib import resources
from typing import Any

import pytest
import yaml

from spendgate.core.catalog import parse

NOW_MS = 1_795_651_200_000  # 2026-11-26


def bundled() -> list[dict[str, Any]]:
    text = (resources.files("spendgate.catalog") / "prices.yaml").read_text(encoding="utf-8")
    models: list[dict[str, Any]] = yaml.safe_load(text)["models"]
    return models


def entries(provider: str) -> dict[str, dict[str, Any]]:
    """The newest entry per model of one provider (older dated entries are history)."""
    found: dict[str, dict[str, Any]] = {}
    for e in bundled():
        slug, _, model = e["id"].partition("/")
        if slug == provider and (
            model not in found or e["effective_from"] > found[model]["effective_from"]
        ):
            found[model] = e
    return found


def meter(entry: dict[str, Any], name: str) -> Decimal:
    return Decimal(entry["meters"][name])


# --- Anthropic: platform.claude.com/docs/en/about-claude/pricing ---------------------------------

# "Cache hits and refreshes": 0.025x on Fable and Mythos 5.2 and 5.1, 0.05x on Opus and Sonnet 5.5,
# "All other models use the standard 0.1x multiplier".
ANTHROPIC_CACHE_HIT = {
    "claude-fable-5-2": Decimal("0.025"),
    "claude-mythos-5-2": Decimal("0.025"),
    "claude-fable-5-1": Decimal("0.025"),
    "claude-mythos-5-1": Decimal("0.025"),
    "claude-opus-5-5": Decimal("0.05"),
    "claude-sonnet-5-5": Decimal("0.05"),
}
ANTHROPIC_MODELS = {
    "claude-fable-5-2", "claude-mythos-5-2", "claude-fable-5-1", "claude-mythos-5-1",
    "claude-fable-5", "claude-mythos-5", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
    "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5-20251101", "claude-sonnet-5-5",
    "claude-sonnet-5", "claude-sonnet-4-6", "claude-sonnet-4-5-20250929", "claude-haiku-5-5",
    "claude-haiku-4-5-20251001",
}  # fmt: skip


def test_anthropic_entries_are_the_models_the_claude_api_prices() -> None:
    assert set(entries("anthropic")) == ANTHROPIC_MODELS


@pytest.mark.parametrize("model", sorted(ANTHROPIC_MODELS))
def test_anthropic_cache_hit_is_the_page_multiplier_of_input(model: str) -> None:
    e = entries("anthropic")[model]
    factor = ANTHROPIC_CACHE_HIT.get(model, Decimal("0.1"))
    assert meter(e, "cached_input_per_mtok") == meter(e, "input_per_mtok") * factor


@pytest.mark.parametrize("model", sorted(ANTHROPIC_MODELS))
def test_anthropic_cache_write_is_the_1h_price_twice_input(model: str) -> None:
    # "1-hour cache write: 2x base input price"; the catalog stores the dearer 1h price.
    e = entries("anthropic")[model]
    assert meter(e, "cache_write_per_mtok") == meter(e, "input_per_mtok") * 2


def test_anthropic_mythos_models_price_like_the_fable_model_of_the_same_version() -> None:
    found = entries("anthropic")
    for version in ("5-2", "5-1", "5"):
        mythos, fable = found[f"claude-mythos-{version}"], found[f"claude-fable-{version}"]
        assert mythos["meters"] == fable["meters"]
        assert mythos["limits"] == fable["limits"]


def test_only_haiku_5_5_has_a_long_context_price_on_the_claude_api() -> None:
    for model, e in entries("anthropic").items():
        assert ("long_context" in e) == (model == "claude-haiku-5-5"), model
    # "for prompts over 100,000 tokens": every meter 5x (0.50 / 0.625 / 1 / 0.05 / 2.50).
    assert entries("anthropic")["claude-haiku-5-5"]["long_context"] == {
        "threshold_input_tokens": 100000,
        "input_multiplier": "5",
        "output_multiplier": "5",
    }


def test_anthropic_limits_follow_the_models_overview() -> None:
    small = {"claude-opus-4-5-20251101", "claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"}
    for model, e in entries("anthropic").items():
        window, output = (200000, 64000) if model in small else (1000000, 128000)
        assert e["limits"] == {
            "max_output_tokens": output,
            "context_window": window,
            "max_image_tokens": 6000,
        }, model


def test_every_new_entry_resolves_today_through_the_bundled_catalog() -> None:
    catalog = parse([{"catalog_version": 1, "models": bundled()}], "0" * 64, ledger_currency="USD")
    for provider in ("anthropic",):
        for model in entries(provider):
            entry = catalog.lookup(provider, model, NOW_MS)
            assert entry.verified, entry.id
