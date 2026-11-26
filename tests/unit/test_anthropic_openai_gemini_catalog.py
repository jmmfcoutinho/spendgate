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


# --- OpenAI: developers.openai.com/api/docs/pricing and /models/<id> ------------------------------

# The pricing page's "Long context (>272K)" table, and every model with a 1.05M context window.
OPENAI_LONG_CONTEXT = {
    "gpt-6.2-astra", "gpt-6.2-sol", "gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna",
    "gpt-6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.5-pro", "gpt-5.4",
    "gpt-5.4-pro",
}  # fmt: skip
# Model pages: "Cached input tokens are priced at 5% (10%) of the uncached input token rate".
OPENAI_CACHED_SHARE = {
    "gpt-6.1-sol": Decimal("0.05"),
    "gpt-6.1-terra": Decimal("0.05"),
    "gpt-6-luna": Decimal("0.1"),
    "gpt-6-sol": Decimal("0.1"),
}


def openai_text() -> dict[str, dict[str, Any]]:
    return {m: e for m, e in entries("openai").items() if e["mode"] == "responses"}


def test_openai_long_context_rule_is_the_page_rule_on_exactly_the_1_05m_models() -> None:
    for model, e in openai_text().items():
        assert ("long_context" in e) == (model in OPENAI_LONG_CONTEXT), model
        assert (e["limits"]["context_window"] == 1050000) == (model in OPENAI_LONG_CONTEXT), model
        if model in OPENAI_LONG_CONTEXT:
            # "input and cache rates double and output costs 1.5 times ... for the full request"
            assert e["long_context"] == {
                "threshold_input_tokens": 272000,
                "input_multiplier": "2",
                "output_multiplier": "1.5",
            }, model


def test_openai_cache_write_is_the_30_minute_price_1_25x_input() -> None:
    # "Cache writes are billed at 1.25x the uncached input token rate" (30-minute retention).
    with_writes = {m: e for m, e in openai_text().items() if "cache_write_per_mtok" in e["meters"]}
    assert set(with_writes) >= OPENAI_LONG_CONTEXT - {
        "gpt-5.5",
        "gpt-5.5-pro",
        "gpt-5.4",
        "gpt-5.4-pro",
    }
    for model, e in with_writes.items():
        assert meter(e, "cache_write_per_mtok") == meter(e, "input_per_mtok") * Decimal("1.25"), (
            model
        )


@pytest.mark.parametrize("model", sorted(OPENAI_CACHED_SHARE))
def test_openai_cached_input_is_the_share_the_model_page_states(model: str) -> None:
    e = openai_text()[model]
    expected = meter(e, "input_per_mtok") * OPENAI_CACHED_SHARE[model]
    assert meter(e, "cached_input_per_mtok") == expected


def test_openai_pro_models_have_no_cached_input_price() -> None:
    # The pricing page lists no cached-input price for them: cached tokens bill at the input price.
    for model, e in openai_text().items():
        assert ("cached_input_per_mtok" in e["meters"]) == ("pro" not in model), model


def test_openai_speech_models_bill_per_character_as_the_adapter_counts() -> None:
    speech = {m: e for m, e in entries("openai").items() if e["mode"] == "speech"}
    assert {m: e["meters"] for m, e in speech.items()} == {
        "tts-1": {"per_1k_characters": "0.015"},  # $15 per 1M characters
        "tts-1-hd": {"per_1k_characters": "0.03"},  # $30 per 1M characters
    }


def test_every_new_entry_resolves_today_through_the_bundled_catalog() -> None:
    catalog = parse([{"catalog_version": 1, "models": bundled()}], "0" * 64, ledger_currency="USD")
    for provider in ("anthropic", "openai"):
        for model in entries(provider):
            entry = catalog.lookup(provider, model, NOW_MS)
            assert entry.verified, entry.id
