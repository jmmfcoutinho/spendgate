"""The Pydantic models and `schema.json` must accept and reject the same documents."""

import copy
import json
from collections.abc import Callable
from importlib import resources
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from spendgate.core.catalog import CatalogError, parse
from spendgate.core.money import FxSnapshot, MissingFxRate

SCHEMA = Draft202012Validator(
    json.loads((resources.files("spendgate.catalog") / "schema.json").read_text(encoding="utf-8"))
)
# Both currencies reach USD, so FX never decides these comparisons.
FX = (FxSnapshot.of("EUR", "USD", "1.1", "2026-11-13", "test"),)

type Mutation = Callable[[dict[str, object]], object]


def base() -> dict[str, object]:
    return {
        "catalog_version": 1,
        "updated": "2026-11-13",
        "providers": {"openai": {"free_statuses": [429]}},
        "models": [
            {
                "id": "openai/m",
                "mode": "chat",
                "currency": "USD",
                "effective_from": "2026-01-01",
                "source_url": "https://example.com/p",
                "verified": True,
                "checked_on": "2026-11-13",
                "note": "n",
                "deployment_type": "standard",
                "region": "swedencentral",
                "price_multiplier": "1.20",
                "meters": {"input_per_mtok": "1", "output_per_mtok": "2"},
                "long_context": {
                    "threshold_input_tokens": 10,
                    "input_multiplier": "2",
                    "output_multiplier": "1.5",
                },
                "limits": {"max_output_tokens": 100, "context_window": 1000},
                "deprecation_date": "2027-01-01",
            }
        ],
    }


def model(d: dict[str, object]) -> dict[str, object]:
    models = d["models"]
    assert isinstance(models, list)
    first: dict[str, object] = models[0]
    return first


def sub(d: dict[str, object], key: str) -> dict[str, object]:
    found = model(d)[key]
    assert isinstance(found, dict)
    return found


def put(key: str, value: object) -> Mutation:
    return lambda d: model(d).__setitem__(key, value)


def drop(key: str) -> Mutation:
    return lambda d: model(d).pop(key)


def put_meter(key: str, value: object) -> Mutation:
    return lambda d: sub(d, "meters").__setitem__(key, value)


def chat_meters(**meters: str) -> Mutation:
    return lambda d: model(d).__setitem__("meters", meters)


def by_mode(mode: str, meters: dict[str, str], *, keep_limits: bool = False) -> Mutation:
    def apply(d: dict[str, object]) -> None:
        model(d).update(mode=mode, meters=meters)
        if not keep_limits:
            model(d).pop("limits")

    return apply


MUTATIONS: dict[str, Mutation] = {
    "unchanged": lambda d: None,
    "version 2": lambda d: d.__setitem__("catalog_version", 2),
    "version missing": lambda d: d.pop("catalog_version"),
    "extra top-level key": lambda d: d.__setitem__("charge_zero", True),
    "updated bad": lambda d: d.__setitem__("updated", "13/11/2026"),
    "no providers block": lambda d: d.pop("providers"),
    "empty models": lambda d: d.__setitem__("models", []),
    "models missing": lambda d: d.pop("models"),
    "free_statuses 529 too": lambda d: d.__setitem__(
        "providers", {"anthropic": {"free_statuses": [429, 529]}}
    ),
    "free_statuses empty": lambda d: d.__setitem__("providers", {"openai": {"free_statuses": []}}),
    "free_statuses 500": lambda d: d.__setitem__("providers", {"openai": {"free_statuses": [500]}}),
    "free_statuses duplicate": lambda d: d.__setitem__(
        "providers", {"openai": {"free_statuses": [429, 429]}}
    ),
    "free_statuses string": lambda d: d.__setitem__(
        "providers", {"openai": {"free_statuses": ["429"]}}
    ),
    "provider block knob": lambda d: d.__setitem__("providers", {"openai": {"charge_zero": True}}),
    "provider block on_missing_usage": lambda d: d.__setitem__(
        "providers", {"openai": {"on_missing_usage": "charge_zero"}}
    ),
    "entry extra key": put("on_missing_usage", "charge_zero"),
    "id without slash": put("id", "gpt"),
    "id hyphen slug": put("id", "azure-openai/m"),
    "id upper slug": put("id", "OpenAI/m"),
    "mode unknown": put("mode", "realtime"),
    "currency GBP": put("currency", "GBP"),
    "currency lower": put("currency", "usd"),
    "date unquoted style": put("effective_from", "2026-1-1"),
    "date missing": drop("effective_from"),
    "source http": put("source_url", "http://example.com"),
    "source missing": drop("source_url"),
    "verified string": put("verified", "yes"),
    "verified missing": drop("verified"),
    "note too long": put("note", "x" * 301),
    "note exactly 300": put("note", "x" * 300),
    "deployment_type unknown": put("deployment_type", "turbo"),
    "region upper": put("region", "SwedenCentral"),
    "regions ok": put("regions", ["swedencentral", "westeurope"]),
    "regions one": put("regions", ["swedencentral"]),
    "regions empty": put("regions", []),
    "regions repeated": put("regions", ["swedencentral", "swedencentral"]),
    "regions upper": put("regions", ["SwedenCentral"]),
    "regions string": put("regions", "swedencentral"),
    "regions number": put("regions", [1]),
    "multiplier zero": put("price_multiplier", "0"),
    "multiplier 0.0": put("price_multiplier", "0.0"),
    "multiplier tiny": put("price_multiplier", "0.001"),
    "multiplier float": put("price_multiplier", 1.2),
    "multiplier negative": put("price_multiplier", "-1"),
    "multiplier leading zero": put("price_multiplier", "01.2"),
    "price float": put_meter("input_per_mtok", 1.0),
    "price int": put_meter("input_per_mtok", 1),
    "price negative": put_meter("input_per_mtok", "-1"),
    "price zero": put_meter("input_per_mtok", "0"),
    "price exponent": put_meter("input_per_mtok", "1e3"),
    "price trailing dot": put_meter("input_per_mtok", "1."),
    "price leading dot": put_meter("input_per_mtok", ".5"),
    "unknown meter": put_meter("per_token", "1"),
    "granularity 0": put_meter("audio_granularity_seconds", 0),
    "granularity 15": put_meter("audio_granularity_seconds", 15),
    "granularity bool": put_meter("audio_granularity_seconds", True),
    "granularity string": put_meter("audio_granularity_seconds", "15"),
    "meters empty": put("meters", {}),
    "chat without output meter": chat_meters(input_per_mtok="1"),
    "chat without input meter": chat_meters(output_per_mtok="1"),
    "chat without limits": drop("limits"),
    "chat limits without ceiling": put("limits", {"context_window": 5}),
    "ceiling zero": put("limits", {"max_output_tokens": 0}),
    "ceiling float": put("limits", {"max_output_tokens": 1.5}),
    "ceiling bool": put("limits", {"max_output_tokens": True}),
    "limits extra": put("limits", {"max_output_tokens": 5, "max_tokens": 5}),
    "image ceiling ok": put("limits", {"max_output_tokens": 5, "max_image_tokens": 3000}),
    "image ceiling zero": put("limits", {"max_output_tokens": 5, "max_image_tokens": 0}),
    "image ceiling string": put("limits", {"max_output_tokens": 5, "max_image_tokens": "9"}),
    "responses mode ok": put("mode", "responses"),
    "messages without limits": lambda d: (model(d).update(mode="messages"), model(d).pop("limits")),
    "embedding ok": by_mode("embedding", {"input_per_mtok": "1"}),
    "embedding without input": by_mode("embedding", {"output_per_mtok": "1"}),
    "transcription per minute": by_mode("transcription", {"audio_input_per_minute": "1"}),
    "transcription per token": by_mode("transcription", {"audio_input_per_mtok": "1"}),
    "transcription neither": by_mode("transcription", {"input_per_mtok": "1"}),
    "speech per chars": by_mode("speech", {"per_1k_characters": "1"}),
    "speech per audio token": by_mode("speech", {"audio_output_per_mtok": "1"}),
    "speech neither": by_mode("speech", {"input_per_mtok": "1"}),
    "long_context missing key": lambda d: sub(d, "long_context").pop("output_multiplier"),
    "long_context zero threshold": lambda d: sub(d, "long_context").__setitem__(
        "threshold_input_tokens", 0
    ),
    "long_context zero multiplier": lambda d: sub(d, "long_context").__setitem__(
        "input_multiplier", "0"
    ),
    "long_context extra": lambda d: sub(d, "long_context").__setitem__("bonus", "1"),
    "long_context absent": drop("long_context"),
    "deprecation bad": put("deprecation_date", "soon"),
    "deprecation absent": drop("deprecation_date"),
}


def schema_accepts(doc: dict[str, object]) -> bool:
    return SCHEMA.is_valid(doc)


def models_accept(doc: dict[str, object]) -> bool:
    try:
        parse([doc], "00" * 32, ledger_currency="USD", fx=FX)
    except (CatalogError, MissingFxRate):
        return False
    return True


@pytest.mark.parametrize("name", list(MUTATIONS))
def test_models_and_schema_agree(name: str) -> None:
    doc = base()
    MUTATIONS[name](doc)
    assert models_accept(doc) == schema_accepts(doc), name


def test_the_corpus_has_both_accepted_and_rejected_documents() -> None:
    verdicts = []
    for mutate in MUTATIONS.values():
        doc = base()
        mutate(doc)
        verdicts.append(schema_accepts(doc))
    assert verdicts.count(True) >= 12
    assert verdicts.count(False) >= 40


@pytest.mark.parametrize("path", ["bundled", "example"])
def test_shipped_documents_pass_both(path: str) -> None:
    if path == "bundled":
        text = (resources.files("spendgate.catalog") / "prices.yaml").read_text(encoding="utf-8")
    else:
        text = (Path(__file__).parents[2] / "docs" / "catalog-example.yaml").read_text(
            encoding="utf-8"
        )
    doc = yaml.safe_load(text)
    assert schema_accepts(doc)
    assert models_accept(doc)


def test_the_models_are_deliberately_stricter_on_provider_slugs_and_duplicates() -> None:
    # Not expressible in JSON Schema: the slug set and (id, effective_from) uniqueness.
    doc = base()
    put("id", "vertex/m")(doc)
    assert schema_accepts(doc)
    assert not models_accept(doc)
    twice = base()
    twice["models"] = [copy.deepcopy(model(base())), copy.deepcopy(model(base()))]
    assert schema_accepts(twice)
    assert not models_accept(twice)
