"""scripts/litellm_prices.py: conversion, mapping, diff classification and drafted entries.

No network: everything reads the hand-made fixture in tests/data/litellm/.
"""
# ruff: noqa: ANN401  (the script is loaded dynamically, so its types are Any here)

import importlib.util
import json
import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "tests" / "data" / "litellm"
TODAY = date(2026, 11, 18)
COMMIT = "a" * 40


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "litellm_prices", REPO / "scripts/litellm_prices.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["litellm_prices"] = module
    spec.loader.exec_module(module)
    return module


lp = load_script()
SCHEMA = Draft202012Validator(
    json.loads((REPO / "src/spendgate/catalog/schema.json").read_text(encoding="utf-8"))
)


def source() -> Any:
    raw = (DATA / "model_prices.json").read_bytes()
    return lp.Source(COMMIT, lp.raw_url(COMMIT), "0" * 64, lp.parse_litellm(raw))


def report(**kw: Any) -> Any:
    args: dict[str, Any] = {"threshold": Decimal(1), "today": TODAY, "horizon_days": 180}
    return lp.diff_catalogs(source(), lp.load_ours(DATA / "catalog.yaml"), **(args | kw))


def findings(rep: Any, entry_id: str) -> dict[str, Any]:
    return {f.what: f for f in rep.findings if f.id == entry_id}


# --- conversion ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("literal", "factor", "expected"),
    [
        ("1e-07", 1_000_000, "0.1"),
        ("1.25e-05", 1_000_000, "12.5"),
        ("2.5e-06", 1_000_000, "2.5"),
        ("0.0", 1_000_000, "0"),
        ("7.5e-05", 60, "0.0045"),
        ("8e-05", 1000, "0.08"),
        ("0.000283333333333", 60, "0.01699999999998"),
        ("1.1e-07", 1_000_000, "0.11"),  # a float would give 0.10999999999999999
    ],
)
def test_conversion_is_exact_decimal_arithmetic(literal: str, factor: int, expected: str) -> None:
    value = json.loads(literal, parse_float=Decimal)
    assert lp.dec_str(lp.scale(value, Decimal(factor))) == expected


def test_dec_str_never_uses_an_exponent_and_matches_the_schema_pattern() -> None:
    pattern = re.compile(
        json.loads((REPO / "src/spendgate/catalog/schema.json").read_text())["$defs"]["decimal"][
            "pattern"
        ]
    )
    for text in ("1E+3", "0.000001000", "100", "0E-8", "12.50"):
        out = lp.dec_str(Decimal(text))
        assert "e" not in out.lower() and pattern.fullmatch(out)


def test_floats_and_booleans_are_not_numbers() -> None:
    with pytest.raises(lp.InputError):
        lp.to_decimal(0.1)
    with pytest.raises(lp.InputError):
        lp.to_decimal(True)


def test_a_non_json_file_is_an_input_error() -> None:
    with pytest.raises(lp.InputError):
        lp.parse_litellm(b"404: Not Found")
    with pytest.raises(lp.InputError):
        lp.parse_litellm(b"[1, 2]")


def test_commit_must_be_a_full_sha() -> None:
    assert lp.raw_url(COMMIT).startswith("https://raw.githubusercontent.com/BerriAI/litellm/aaaa")
    for bad in ("main", "abc123", "../x", COMMIT.upper()):
        with pytest.raises(lp.InputError):
            lp.raw_url(bad)


def test_the_default_commit_is_pinned() -> None:
    assert len(lp.DEFAULT_COMMIT) == 40 and lp.raw_url(lp.DEFAULT_COMMIT).endswith(lp.FILE_PATH)


# --- mapping ---------------------------------------------------------------------------------


def test_mapping_table() -> None:
    data = source().data
    mapped = {k: lp.map_key(k, v) for k, v in data.items()}
    ids = {k: (m.id, m.variant) for k, m in mapped.items() if m is not None}
    assert ids == {
        "gpt-6-luna": ("openai/gpt-6-luna", "base"),
        "gpt-transcribe": ("openai/gpt-transcribe", "base"),
        "whisper-1": ("openai/whisper-1", "base"),
        "text-embedding-3-small": ("openai/text-embedding-3-small", "base"),
        "gpt-6-new": ("openai/gpt-6-new", "base"),
        "gpt-6-nolimit": ("openai/gpt-6-nolimit", "base"),
        "azure/gpt-6-luna": ("azure_openai/gpt-6-luna", "base"),
        "azure/eu/gpt-6-luna": ("azure_openai/gpt-6-luna", "zone"),
        "azure/us/gpt-6-luna": ("azure_openai/gpt-6-luna", "zone"),
        "azure/whisper": ("azure_openai/whisper", "base"),
        "claude-fable-5-2": ("anthropic/claude-fable-5-2", "base"),
        "global.anthropic.claude-fable-5-2": ("bedrock/global.anthropic.claude-fable-5-2", "base"),
        "gemini/gemini-4-carbon": ("google/gemini-4-carbon", "base"),
        "elevenlabs/eleven_v4": ("elevenlabs/eleven_v4", "base"),
    }
    # Images, other providers, Vertex, US Gov and region-qualified keys are out of scope.
    assert mapped["gpt-image-1"] is None and mapped["vertex_ai/gemini-4-carbon"] is None
    assert mapped["azure/us-gov/gpt-6-luna"] is None
    assert mapped["bedrock/us-east-1/anthropic.claude-fable-5-2"] is None
    assert mapped["some-other-provider/model"] is None and mapped["claude-fable-latest"] is None


def test_meter_conversion_per_provider() -> None:
    data = source().data
    assert {
        k: lp.dec_str(v) for k, v in lp.convert_meters("openai", data["gpt-6-luna"]).items()
    } == {
        "input_per_mtok": "0.1",
        "cached_input_per_mtok": "0.01",
        "cache_write_per_mtok": "0.125",
        "output_per_mtok": "0.5",
    }
    anthropic = lp.convert_meters("anthropic", data["claude-fable-5-2"])
    assert lp.dec_str(anthropic["cache_write_per_mtok"]) == "16"  # the 1-hour price, not 10
    assert (
        lp.dec_str(lp.convert_meters("openai", data["gpt-transcribe"])["audio_input_per_minute"])
        == "0.0045"
    )
    assert (
        lp.dec_str(
            lp.convert_meters("elevenlabs", data["elevenlabs/eleven_v4"])["per_1k_characters"]
        )
        == "0.08"
    )


def test_long_context_is_read_as_whole_request_multipliers() -> None:
    (lc,) = lp.long_contexts(source().data["gpt-6-luna"])
    assert (lc.threshold, lc.input_multiplier, lc.output_multiplier) == (272000, 2, Decimal("1.5"))


# --- diff classification ---------------------------------------------------------------------


def test_summary_classification() -> None:
    rep = report()
    assert sorted(rep.exact) == [
        "anthropic/claude-fable-5-2",
        "azure_openai/gpt-6-luna",
        "bedrock/global.anthropic.claude-fable-5-2",
        "elevenlabs/eleven_v4",
        "google/gemini-4-carbon",
        "openai/gpt-6-luna",
        "openai/whisper-1",
    ]
    assert rep.tolerance == ["openai/text-embedding-3-small"]
    assert sorted(rep.differ) == ["azure_openai/whisper", "openai/gpt-transcribe"]
    assert rep.ours_only == {"azure_speech/azure-speech": "LiteLLM has no equivalent provider"}
    assert rep.theirs_only == ["openai/gpt-6-new", "openai/gpt-6-nolimit"]
    assert rep.skipped == ["google/gemini-4-carbon@2026-12-01"]  # the future entry is not compared


def test_a_lower_price_than_litellm_is_reported_with_both_values() -> None:
    f = findings(report(), "openai/gpt-transcribe")["audio_input_per_minute"]
    assert (f.ours, f.theirs, f.failing, f.kind) == ("0.004", "0.0045", True, "price")
    assert f.pct == Decimal("12.5")  # (0.0045 - 0.004) / 0.004


def test_a_higher_price_is_a_difference_too_but_marked_as_such() -> None:
    f = findings(report(), "azure_openai/whisper")["audio_input_per_minute"]
    assert f.failing and f.pct is not None and f.pct < 0
    text = lp.render(report())
    assert "ours HIGHER" in text and "ours LOWER" in text


def test_within_threshold_is_equal_unless_the_threshold_is_tightened() -> None:
    assert "openai/text-embedding-3-small" in report().tolerance
    tight = report(threshold=Decimal("0.1"))
    assert "openai/text-embedding-3-small" in tight.differ


def test_data_zone_multiplier_is_checked_as_a_ceiling() -> None:
    assert not [f for f in report().findings if f.kind == "ceiling"]
    ours = lp.load_ours(DATA / "catalog.yaml")
    low = [
        e
        if e.id != "azure_openai/gpt-6-luna"
        else lp.Ours(
            e.id, e.effective_from, e.meters, Decimal("1.05"), e.long_context, e.deprecation, e.raw
        )
        for e in ours
    ]
    rep = lp.diff_catalogs(source(), low, threshold=Decimal(1), today=TODAY, horizon_days=180)
    ceiling = [f for f in rep.findings if f.kind == "ceiling"]
    assert {f.what for f in ceiling} == {"input_per_mtok", "output_per_mtok"}
    assert (
        ceiling[0].ours == "0.105"
        and ceiling[0].theirs == "0.12"
        and "azure_openai/gpt-6-luna" in rep.differ
    )


def test_deprecation_date_litellm_has_and_we_do_not_fails_within_the_horizon() -> None:
    rep = report()
    dep = findings(rep, "openai/whisper-1")["deprecation_date"]
    assert (dep.ours, dep.theirs, dep.failing) == ("none", "2027-02-26", True)
    assert "openai/whisper-1" in rep.exact  # the price itself matches
    far = findings(report(horizon_days=30), "openai/whisper-1")["deprecation_date"]
    assert not far.failing and "informational" in far.note
    same = findings(rep, "azure_openai/whisper")
    assert "deprecation_date" not in same  # we carry the same date


def test_missing_long_context_and_missing_meter_are_flagged() -> None:
    ours = [e for e in lp.load_ours(DATA / "catalog.yaml") if e.id == "openai/gpt-6-luna"]
    no_lc = [
        lp.Ours(e.id, e.effective_from, e.meters, e.multiplier, None, None, e.raw) for e in ours
    ]
    rep = lp.diff_catalogs(source(), no_lc, threshold=Decimal(1), today=TODAY, horizon_days=180)
    (f,) = [x for x in rep.findings if x.kind == "long_context"]
    assert f.failing and "we do not carry" in f.note
    extra = {**ours[0].meters, "audio_input_per_minute": Decimal("1")}
    odd = [
        lp.Ours(e.id, e.effective_from, extra, e.multiplier, e.long_context, None, e.raw)
        for e in ours
    ]
    rep = lp.diff_catalogs(source(), odd, threshold=Decimal(1), today=TODAY, horizon_days=180)
    (m,) = [x for x in rep.findings if x.what == "audio_input_per_minute"]
    assert not m.failing and m.theirs == "-"


def test_main_exit_codes_and_report_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "diff.txt"
    base = [
        "diff",
        "--commit",
        COMMIT,
        "--file",
        str(DATA / "model_prices.json"),
        "--today",
        "2026-11-18",
    ]
    assert lp.main([*base, "--catalog", str(DATA / "catalog.yaml"), "--out", str(out)]) == 1
    assert out.read_text(encoding="utf-8") == capsys.readouterr().out
    clean = tmp_path / "clean.yaml"
    clean.write_text(
        yaml.safe_dump(
            {
                "catalog_version": 1,
                "models": [
                    e
                    for e in yaml.safe_load((DATA / "catalog.yaml").read_text(encoding="utf-8"))[
                        "models"
                    ]
                    if e["id"] in {"openai/gpt-6-luna", "elevenlabs/eleven_v4"}
                ],
            }
        ),
        encoding="utf-8",
    )
    assert lp.main([*base, "--catalog", str(clean)]) == 0
    assert lp.main([*base, "--file", str(tmp_path / "missing.json")]) == 2


# --- import and override ---------------------------------------------------------------------


def draft(key: str) -> dict[str, Any]:
    spec = source().data[key]
    mapped = lp.map_key(key, spec)
    entry: dict[str, Any] = lp.draft_entry(mapped, source(), TODAY)
    return entry


def test_drafted_entries_validate_and_are_never_verified() -> None:
    for key in (
        "gpt-6-luna",
        "gpt-transcribe",
        "azure/gpt-6-luna",
        "claude-fable-5-2",
        "global.anthropic.claude-fable-5-2",
        "gemini/gemini-4-carbon",
        "elevenlabs/eleven_v4",
        "text-embedding-3-small",
        "gpt-6-new",
        "whisper-1",
    ):
        entry = draft(key)
        assert not list(SCHEMA.iter_errors({"catalog_version": 1, "models": [entry]})), key
        assert entry["verified"] is False and entry["checked_on"] == "2026-11-18"
        assert entry["source_url"] == lp.raw_url(COMMIT)
        assert "LiteLLM" in entry["note"] and "verified: true" in entry["note"]


def test_drafted_entry_content() -> None:
    alpha = draft("gpt-6-luna")
    assert alpha["mode"] == "chat" and alpha["id"] == "openai/gpt-6-luna"
    assert alpha["meters"]["output_per_mtok"] == "0.5"
    assert alpha["long_context"] == {
        "threshold_input_tokens": 272000,
        "input_multiplier": "2",
        "output_multiplier": "1.5",
    }
    assert alpha["limits"] == {"max_output_tokens": 128000, "context_window": 1050000}
    assert draft("claude-fable-5-2")["mode"] == "messages"
    assert draft("global.anthropic.claude-fable-5-2")["deployment_type"] == "global"
    assert draft("whisper-1")["deprecation_date"] == "2027-02-26"
    # Reasoning priced like output adds nothing.
    assert "reasoning_per_mtok" not in draft("gemini/gemini-4-carbon")["meters"]


def test_import_prints_yaml_and_reports_what_it_cannot_draft() -> None:
    text, problems = lp.run_import(
        source(), ["gpt-6-luna", "gpt-6-nolimit", "nope", "azure/eu/gpt-6-luna"], TODAY
    )
    doc = yaml.safe_load(text)
    assert [m["id"] for m in doc["models"]] == ["openai/gpt-6-luna"]
    assert text.startswith("# Drafted from LiteLLM")
    assert len(problems) == 3
    assert "gpt-6-nolimit" in problems[0] and "limits" in problems[0]
    assert "nope" in problems[1] and "azure/eu/gpt-6-luna" in problems[2]


def test_import_writes_only_where_told(tmp_path: Path) -> None:
    out = tmp_path / "x.yaml"
    args = ["--commit", COMMIT, "--file", str(DATA / "model_prices.json")]
    cat = ["--catalog", str(DATA / "catalog.yaml")]
    assert lp.main(["import", "gpt-6-luna", *args, *cat, "--out", str(out)]) == 0
    assert yaml.safe_load(out.read_text(encoding="utf-8"))["models"][0]["verified"] is False
    assert lp.main(["import", "gpt-6-nolimit", *args, *cat]) == 1


def test_override_holds_every_missing_mappable_model_unverified() -> None:
    text, skipped = lp.run_override(source(), lp.load_ours(DATA / "catalog.yaml"), TODAY)
    doc = yaml.safe_load(text)
    assert [m["id"] for m in doc["models"]] == ["openai/gpt-6-new"]
    assert skipped == ["gpt-6-nolimit"]  # no max_output_tokens: cannot be a valid chat entry
    assert all(m["verified"] is False for m in doc["models"])
    assert "verified: false" in text.split("catalog_version")[0] and "wrong or zero price" in text
    assert not list(SCHEMA.iter_errors(doc))


def test_the_bundled_catalog_diffs_against_the_fixture_without_error() -> None:
    rep = lp.diff_catalogs(
        source(), lp.load_ours(lp.CATALOG), threshold=Decimal(1), today=TODAY, horizon_days=180
    )
    assert len(rep.ours_only) >= 1


# --- reviewed allow-list ---------------------------------------------------------------------


def item(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "azure_openai/whisper",
        "meters": ["audio_input_per_minute"],
        "relation": "ours_higher",
        "reason": "upper bound",
        "accepted_by": "tester",
        "accepted_on": "2026-11-01",
        "expires": "2027-01-30",
    }
    return base | over


def dep_item(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "openai/whisper-1",
        "litellm_date": "2027-02-26",
        "status": "unconfirmed",
        "reason": "only LiteLLM lists it",
        "accepted_by": "tester",
        "accepted_on": "2026-11-01",
        "expires": "2027-01-30",
    }
    return base | over


def accepted(tmp_path: Path, differences: list[Any], deprecations: list[Any] | None = None) -> Any:
    path = tmp_path / "accepted.yaml"
    path.write_text(
        yaml.safe_dump({"differences": differences, "deprecations": deprecations or []}),
        encoding="utf-8",
    )
    return lp.load_accepted(path)


def test_an_accepted_ours_higher_difference_stops_failing(tmp_path: Path) -> None:
    rep = report(accepted=accepted(tmp_path, [item()]))
    assert "azure_openai/whisper" in rep.accepted_ids and "azure_openai/whisper" not in rep.differ
    f = findings(rep, "azure_openai/whisper")["audio_input_per_minute"]
    assert not f.failing and "upper bound" in f.accepted and "tester" in f.accepted
    assert "ACCEPTED DIFFERENCES" in lp.render(rep)
    assert report().differ.count("azure_openai/whisper") == 1  # not accepted without the list


def test_ours_lower_is_never_accepted(tmp_path: Path) -> None:
    rep = report(accepted=accepted(tmp_path, [item(id="openai/gpt-transcribe")]))
    assert "openai/gpt-transcribe" in rep.differ
    assert findings(rep, "openai/gpt-transcribe")["audio_input_per_minute"].failing
    assert rep.unused_acceptances == ["openai/gpt-transcribe [audio_input_per_minute]"]


def test_the_relation_must_be_ours_higher(tmp_path: Path) -> None:
    for relation in ("ours_lower", "any", None):
        with pytest.raises(lp.InputError, match="ours_higher"):
            accepted(tmp_path, [item(relation=relation)])


def test_an_expired_acceptance_fails_again(tmp_path: Path) -> None:
    rep = report(
        accepted=accepted(tmp_path, [item(accepted_on="2026-08-01", expires="2026-10-30")])
    )
    f = findings(rep, "azure_openai/whisper")["audio_input_per_minute"]
    assert f.failing and "acceptance expired on 2026-10-30" in f.note
    assert "azure_openai/whisper" in rep.differ


def test_an_acceptance_covers_only_the_listed_meters(tmp_path: Path) -> None:
    rep = report(accepted=accepted(tmp_path, [item(id="azure_openai/gpt-4o-transcribe")]))
    assert "azure_openai/gpt-4o-transcribe" not in rep.accepted_ids  # not in the fixture: unused
    assert rep.unused_acceptances


@pytest.mark.parametrize(
    "bad",
    [
        {"expires": "2027-03-01"},  # more than 90 days after accepted_on
        {"expires": "2026-11-01"},  # not after accepted_on
        {"accepted_by": ""},
        {"reason": " "},
        {"meters": ["not_a_meter"]},
        {"meters": []},
    ],
)
def test_malformed_allow_list_items_are_refused(tmp_path: Path, bad: dict[str, Any]) -> None:
    with pytest.raises(lp.InputError):
        accepted(tmp_path, [item(**bad)])


def test_a_missing_allow_list_accepts_nothing(tmp_path: Path) -> None:
    assert lp.load_accepted(tmp_path / "none.yaml") == []


def test_unconfirmed_deprecations_are_accepted_by_exact_date(tmp_path: Path) -> None:
    rep = report(accepted=accepted(tmp_path, [], [dep_item()]))
    dep = findings(rep, "openai/whisper-1")["deprecation_date"]
    assert not dep.failing and dep.accepted and "UNCONFIRMED" in lp.render(rep)
    changed = report(accepted=accepted(tmp_path, [], [dep_item(litellm_date="2027-01-01")]))
    assert findings(changed, "openai/whisper-1")["deprecation_date"].failing  # the date moved
    gone = report(
        accepted=accepted(tmp_path, [], [dep_item(accepted_on="2026-08-01", expires="2026-10-30")])
    )
    assert findings(gone, "openai/whisper-1")["deprecation_date"].failing


def test_deprecation_items_need_the_unconfirmed_status(tmp_path: Path) -> None:
    with pytest.raises(lp.InputError):
        accepted(tmp_path, [], [dep_item(status="confirmed")])


def test_the_shipped_allow_list_is_valid_and_has_no_ours_lower_item() -> None:
    items = lp.load_accepted(lp.ACCEPTED)
    assert len(items) >= 11
    assert all((a.expires - a.accepted_on).days <= lp.MAX_ACCEPTANCE_DAYS for a in items)


def test_main_uses_the_allow_list(tmp_path: Path) -> None:
    base = ["diff", "--commit", COMMIT, "--file", str(DATA / "model_prices.json")]
    base += ["--today", "2026-11-18", "--catalog", str(DATA / "catalog.yaml")]
    path = tmp_path / "a.yaml"
    path.write_text(yaml.safe_dump({"differences": [item()]}), encoding="utf-8")
    assert lp.main([*base, "--accepted", str(path)]) == 1  # gpt-transcribe is ours lower
