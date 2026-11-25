"""Entries with `verified: false` are named once at start (serve, library mode, check-config)."""

import logging
from pathlib import Path

import pytest

from spendgate.cli import serve
from spendgate.config import check_config, load
from spendgate.core.money import Money
from spendgate.local.runtime import Runtime
from tests.integration.harness import async_test
from tests.unit.test_config import BASE, ENV, write

ENTRY = """\
  - {{id: openai/draft-{n}, mode: embedding, currency: USD, effective_from: "2026-11-18",
     source_url: "https://x.example/p", verified: false, meters: {{input_per_mtok: "1"}}}}
"""


def with_override(tmp_path: Path, count: int) -> Path:
    override = tmp_path / "o.yaml"
    body = "".join(ENTRY.format(n=n) for n in range(count))
    override.write_text(f"catalog_version: 1\nmodels:\n{body}", encoding="utf-8")
    return write(tmp_path, BASE + "catalog: {paths: [o.yaml]}\n")


def test_the_bundled_catalog_has_nothing_to_warn_about(tmp_path: Path) -> None:
    settings = load(write(tmp_path), env=ENV)
    assert settings.unverified_summary() is None
    assert check_config(write(tmp_path), env=ENV).unverified is None


def test_the_summary_names_the_count_and_the_first_few_ids(tmp_path: Path) -> None:
    settings = load(with_override(tmp_path, 7), env=ENV)
    line = settings.unverified_summary()
    assert line is not None
    assert line.startswith("7 catalog entries have unverified prices")
    assert "openai/draft-0@2026-11-18" in line and "openai/draft-4@2026-11-18" in line
    assert "draft-5" not in line and line.endswith("and 2 more")


def test_one_unverified_entry_reads_in_the_singular(tmp_path: Path) -> None:
    line = load(with_override(tmp_path, 1), env=ENV).unverified_summary()
    assert line is not None and line.startswith("1 catalog entry has unverified prices")
    assert "more" not in line


def test_check_config_reports_it_and_stays_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    path = with_override(tmp_path, 2)
    assert check_config(path, env=ENV).unverified is not None
    assert serve.run_check_config(str(path)) == 0
    captured = capsys.readouterr()
    assert "warning: 2 catalog entries have unverified prices" in captured.err
    assert "configuration ok" in captured.out


@async_test
async def test_library_mode_logs_one_warning_at_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = load(with_override(tmp_path, 3), env=ENV)
    runtime = Runtime(settings, tmp_path / "local.db", Money.from_amount("10", "USD"))
    with caplog.at_level(logging.WARNING, logger="spendgate.local"):
        await runtime.start()
        await runtime.start()  # idempotent: still one warning
    await runtime.aclose()
    warnings = [r.getMessage() for r in caplog.records if "unverified" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].startswith("3 catalog entries have unverified")


SHADOW = """\
catalog_version: 1
models:
  - {{id: openai/gpt-6-luna, mode: responses, currency: USD, effective_from: "{day}",
     source_url: "https://x.example/p", verified: false,
     meters: {{input_per_mtok: "1", output_per_mtok: "2"}}, limits: {{max_output_tokens: 10}}}}
"""


@pytest.mark.parametrize("day", ["2026-11-13", "2026-12-01"])  # same date as bundled, and later
def test_an_unverified_entry_superseding_a_verified_one_is_a_check_config_warning(
    tmp_path: Path,
    day: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "o.yaml").write_text(SHADOW.format(day=day), encoding="utf-8")
    path = write(tmp_path, BASE + "catalog: {paths: [o.yaml]}\n")
    report = check_config(path, env=ENV)
    assert report.ok and report.superseded == (
        "1 unverified catalog entry supersedes a verified price of the same id: "
        f"openai/gpt-6-luna@{day}"
    )
    assert serve.run_check_config(str(path)) == 0
    assert "warning: 1 unverified catalog entry supersedes" in capsys.readouterr().err


def test_new_unverified_ids_and_the_bundled_catalog_supersede_nothing(tmp_path: Path) -> None:
    assert check_config(write(tmp_path), env=ENV).superseded is None
    assert check_config(with_override(tmp_path, 7), env=ENV).superseded is None
