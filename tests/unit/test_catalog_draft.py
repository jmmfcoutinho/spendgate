"""`spendgate catalog draft`: a verified: false stand-in priced like another model (ADR-0028).

No network and no secrets: the bundled catalog and override files under tmp_path are read, the
clock is passed in.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from spendgate.cli import catalog, main
from spendgate.cli.client import EXIT_CONFIG, EXIT_OK, EXIT_USAGE, CliError
from spendgate.cli.entry import build_parser
from spendgate.config import check_config
from spendgate.config.yamlio import load_yaml_bytes
from spendgate.core import standin
from spendgate.core.catalog import Catalog, PriceEntry, parse
from tests.unit.test_catalog import ms
from tests.unit.test_config import BASE, ENV, write

TODAY = date(2026, 11, 26)
NOON = ms(TODAY.isoformat())
LIKE = "anthropic/claude-haiku-5-5"  # bundled, with cache meters, a long-context tier and limits
NEW = "anthropic/claude-q4-test"


def run(*argv: str, today: date = TODAY) -> int:
    args = build_parser().parse_args(["catalog", "draft", *argv])
    try:
        return catalog.run_draft(args, today=today)
    except CliError as exc:
        return exc.code


def models(path: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = yaml.safe_load(path.read_text(encoding="utf-8"))["models"]
    return found


def loaded(*files: Path) -> Catalog:
    """The drafted files over the bundled catalog, the way check-config loads overrides."""
    docs: list[dict[str, Any]] = [catalog._bundled()]
    for f in files:
        doc = load_yaml_bytes(f.read_bytes())
        assert isinstance(doc, dict)
        docs.append(doc)
    return parse(docs, "0" * 64, ledger_currency="USD")


def bundled(catalog_id: str) -> PriceEntry:
    provider, _, model = catalog_id.partition("/")
    return loaded().lookup(provider, model, NOON)


def prices(entry: PriceEntry) -> dict[str, Decimal]:
    dumped = entry.meters.model_dump(exclude_none=True)
    return {k: v for k, v in dumped.items() if isinstance(v, Decimal)}


def override(tmp_path: Path, *entries: str, extra: str = "") -> Path:
    """An override file `own.yaml` and a config listing it (schema-valid, never served)."""
    (tmp_path / "own.yaml").write_text(
        "catalog_version: 1\nmodels:\n" + "".join(entries), encoding="utf-8"
    )
    return write(tmp_path, BASE + "catalog: {paths: [own.yaml]}\n" + extra)


def own(model: str, day: str, meters: str, *, mode: str = "chat", more: str = "") -> str:
    limits = "    limits: {max_output_tokens: 4096}\n" if mode == "chat" else ""
    return (
        f"  - id: {model}\n    mode: {mode}\n    currency: {more or 'USD'}\n"
        f'    effective_from: "{day}"\n    source_url: https://example.com/pricing\n'
        f"    verified: true\n    meters: {meters}\n{limits}"
    )


# --- what is drafted ---------------------------------------------------------------------------


def test_m10_the_stand_in_carries_every_meter_times_the_multiplier_exactly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "stand-in.yaml"
    assert run("--like", LIKE, "--as", NEW, "--multiplier", "1.25", "--out", str(out)) == EXIT_OK
    source = bundled(LIKE)
    drafted = loaded(out).lookup("anthropic", "claude-q4-test", NOON)
    assert prices(drafted) == {k: v * Decimal("1.25") for k, v in prices(source).items()}
    assert prices(drafted) and all(v >= prices(source)[k] for k, v in prices(drafted).items())
    assert drafted.long_context == source.long_context is not None
    assert drafted.limits == source.limits is not None
    assert (drafted.mode, drafted.currency) == (source.mode, source.currency)
    assert drafted.price_multiplier == source.price_multiplier
    assert drafted.verified is False
    assert drafted.effective_from == drafted.checked_on == TODAY
    assert drafted.source_url == source.source_url
    assert drafted.note == (
        f"Stand-in: priced like {source.price_id} x 1.25; replace with the provider's price"
    )
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Stand-in drafted by `spendgate catalog draft` on 2026-11-26")
    assert all(isinstance(v, str) for v in models(out)[0]["meters"].values())  # never a float
    assert "wrote a stand-in for anthropic/claude-q4-test" in capsys.readouterr().out


def test_the_default_multiplier_is_1(tmp_path: Path) -> None:
    out = tmp_path / "stand-in.yaml"
    assert run("--like", LIKE, "--as", NEW, "--out", str(out)) == EXIT_OK
    assert prices(loaded(out).lookup("anthropic", "claude-q4-test", NOON)) == prices(bundled(LIKE))
    assert " x 1;" in models(out)[0]["note"]


def test_a_price_multiplier_is_carried_and_a_billing_step_is_never_multiplied(
    tmp_path: Path,
) -> None:
    config = override(
        tmp_path,
        own("openai/own-dz", "2026-11-01", '{input_per_mtok: "1", output_per_mtok: "4"}')
        + "    price_multiplier: '1.1'\n",
        own(
            "openai/own-stt",
            "2026-11-01",
            '{audio_input_per_minute: "0.006", audio_granularity_seconds: 15}',
            mode="transcription",
        ),
    )
    dz, stt = tmp_path / "dz.yaml", tmp_path / "stt.yaml"
    flags = ("--config", str(config), "--multiplier", "2")
    assert run("--like", "openai/own-dz", "--as", "openai/new-dz", "--out", str(dz), *flags) == 0
    assert run("--like", "openai/own-stt", "--as", "openai/new-stt", "--out", str(stt), *flags) == 0
    [entry] = models(dz)
    assert entry["price_multiplier"] == "1.1"
    assert entry["meters"] == {"input_per_mtok": "2", "output_per_mtok": "8"}
    assert models(stt)[0]["meters"] == {
        "audio_input_per_minute": "0.012",
        "audio_granularity_seconds": 15,
    }


def test_scheduled_prices_of_the_copied_model_are_followed(tmp_path: Path) -> None:
    config = override(
        tmp_path,
        own("openai/own", "2026-11-01", '{input_per_mtok: "1", output_per_mtok: "4"}'),
        own("openai/own", "2026-12-01", '{input_per_mtok: "2", output_per_mtok: "8"}'),
        own("openai/own", "2026-10-01", '{input_per_mtok: "9", output_per_mtok: "9"}'),
    )
    out = tmp_path / "stand-in.yaml"
    argv = ("--like", "openai/own", "--as", "openai/new", "--multiplier", "1.5")
    assert run(*argv, "--config", str(config), "--out", str(out)) == EXIT_OK
    drafted = models(out)
    assert [e["effective_from"] for e in drafted] == ["2026-11-26", "2026-12-01"]
    assert [e["meters"]["input_per_mtok"] for e in drafted] == ["1.5", "3"]
    assert "openai/own@2026-12-01 x 1.5" in drafted[1]["note"]


def test_without_config_only_the_bundled_catalog_is_read(tmp_path: Path) -> None:
    config = override(
        tmp_path, own("openai/own", "2026-11-01", '{input_per_mtok: "1", output_per_mtok: "4"}')
    )
    out = tmp_path / "stand-in.yaml"
    assert run("--like", "openai/own", "--as", "openai/new", "--out", str(out)) == EXIT_USAGE
    assert not out.exists()
    argv = ("--like", "openai/own", "--as", "openai/new", "--out", str(out))
    assert run(*argv, "--config", str(config)) == EXIT_OK


def test_a_currency_other_than_the_ledgers_is_checked_with_the_configs_fx(tmp_path: Path) -> None:
    fx = "fx: {rates: {EUR: '1.1'}, as_of: '2026-11-13', source: s}\n"
    meters = '{input_per_mtok: "1", output_per_mtok: "4"}'
    config = override(tmp_path, own("openai/own-eur", "2026-11-01", meters, more="EUR"), extra=fx)
    out = tmp_path / "stand-in.yaml"
    argv = ("--like", "openai/own-eur", "--as", "openai/new-eur", "--out", str(out))
    assert run(*argv, "--config", str(config)) == EXIT_OK
    assert models(out)[0]["currency"] == "EUR"


def test_an_id_that_is_already_priced_is_allowed_and_warned_about(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "stand-in.yaml"
    luna = "openai/gpt-6-luna"
    assert run("--like", luna, "--as", luna, "--multiplier", "1.5", "--out", str(out)) == 0
    assert f"warning: {luna} is already priced" in capsys.readouterr().err
    assert loaded(out).unverified_over_verified() == (f"{luna}@2026-11-26",)


def test_the_drafted_file_passes_check_config_as_an_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    argv = ["catalog", "draft", "--like", "openai/gpt-6-luna", "--as", "openai/gpt-q4-test"]
    assert main([*argv, "--out", "stand-in.yaml"]) == EXIT_OK
    config = write(
        tmp_path,
        BASE.replace("openai/gpt-6-luna", "openai/gpt-q4-test")
        + "catalog: {paths: [stand-in.yaml]}\n",
    )
    report = check_config(config, env=ENV)
    assert report.ok, report.errors
    assert report.unverified is not None and "openai/gpt-q4-test@" in report.unverified


# --- what is refused ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "multiplier",
    [
        "0.99",
        "0",
        "0.0",
        "-1",
        "-1.5",
        "abc",
        "",
        "1e2",
        "NaN",
        "Infinity",
        "1.",
        ".5",
        "01.5",
        "1,5",
        " 1",
        "1" * 21,
    ],
)
def test_m10_a_multiplier_below_one_or_not_a_decimal_is_refused(
    tmp_path: Path, multiplier: str, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "stand-in.yaml"
    argv = ["--like", LIKE, "--as", NEW, f"--multiplier={multiplier}", "--out", str(out)]
    assert main(["catalog", "draft", *argv]) == EXIT_USAGE
    assert not out.exists()
    assert "error: --multiplier: " in capsys.readouterr().err


def test_m10_a_multiplier_below_one_is_refused_by_the_parser() -> None:
    for text in ("0.5", "0", "-2", "x"):
        with pytest.raises(standin.StandInError):
            standin.parse_multiplier(text)
    assert standin.parse_multiplier("1") == 1
    assert standin.parse_multiplier("1.000001") == Decimal("1.000001")


def test_m10_a_product_that_would_need_rounding_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(standin, "_PRECISION", 3)
    out = tmp_path / "stand-in.yaml"
    assert run("--like", LIKE, "--as", NEW, "--multiplier", "1.0001", "--out", str(out)) == (
        EXIT_CONFIG
    )
    assert not out.exists()


@pytest.mark.parametrize("like", ["openai/no-such-model", "anthropic/claude-q4-test"])
def test_an_unknown_like_is_refused(tmp_path: Path, like: str) -> None:
    out = tmp_path / "stand-in.yaml"
    provider = like.partition("/")[0]
    assert run("--like", like, "--as", f"{provider}/x", "--out", str(out)) == EXIT_USAGE
    assert not out.exists()


def test_a_like_whose_price_is_not_in_force_yet_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "stand-in.yaml"
    old = date(2020, 1, 1)
    assert run("--like", LIKE, "--as", NEW, "--out", str(out), today=old) == EXIT_USAGE


@pytest.mark.parametrize(
    ("like", "as_id"),
    [
        (LIKE, "openai/claude-q4-test"),  # another provider
        ("claude-haiku-5-5", NEW),  # not a catalog id
        (LIKE, "anthropic/"),
        (LIKE, "Anthropic/x"),
    ],
)
def test_a_provider_mismatch_or_a_malformed_id_is_refused(
    tmp_path: Path, like: str, as_id: str, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "stand-in.yaml"
    assert main(["catalog", "draft", "--like", like, "--as", as_id, "--out", str(out)]) == (
        EXIT_USAGE
    )
    assert not out.exists()
    assert capsys.readouterr().err.startswith("error: --")


def test_a_config_that_does_not_load_is_a_config_error(tmp_path: Path) -> None:
    config = write(tmp_path, BASE + "catalog: {paths: [missing.yaml]}\n")
    out = tmp_path / "stand-in.yaml"
    argv = ("--like", LIKE, "--as", NEW, "--out", str(out))
    assert run(*argv, "--config", str(config)) == EXIT_CONFIG
    assert not out.exists()


def test_an_existing_file_is_never_overwritten_without_force(tmp_path: Path) -> None:
    out = tmp_path / "stand-in.yaml"
    out.write_text("# reviewed by hand\n", encoding="utf-8")
    assert run("--like", LIKE, "--as", NEW, "--out", str(out)) == EXIT_USAGE
    assert out.read_text(encoding="utf-8") == "# reviewed by hand\n"
    assert run("--like", LIKE, "--as", NEW, "--out", str(out), "--force") == EXIT_OK
    assert [e["id"] for e in models(out)] == [NEW]


def test_force_replaces_the_file_in_one_step_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "stand-in.yaml"
    out.write_text("# old\n", encoding="utf-8")
    real = Path.replace

    def crash(self: Path, target: Any) -> Path:  # noqa: ANN401 - mirrors Path.replace
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "replace", crash)
    assert run("--like", LIKE, "--as", NEW, "--out", str(out), "--force") == EXIT_CONFIG
    assert out.read_text(encoding="utf-8") == "# old\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["stand-in.yaml"]
    monkeypatch.setattr(Path, "replace", real)
    assert run("--like", LIKE, "--as", NEW, "--out", str(out), "--force") == EXIT_OK
    assert sorted(p.name for p in tmp_path.iterdir()) == ["stand-in.yaml"]
    assert models(out)[0]["id"] == NEW
