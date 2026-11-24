"""ADR-0018 on the command line: `serve` migrates only when told to; `migrate` upgrades."""

from pathlib import Path

import psycopg
import pytest

from spendgate.cli import main, serve
from spendgate.cli.client import EXIT_OK, EXIT_UNAVAILABLE
from spendgate.config import load
from spendgate.ledger import migrations
from spendgate.ledger.sqlite import SqliteLedger
from tests.contract.rig import workdir_location
from tests.integration.harness import CATALOG_OVERRIDE, CONFIG, ENV, MODELS

SQLITE_BACKEND = "backend: {kind: sqlite, path: ./ledger.db}"
REAL_LOAD = migrations.load


def write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: str) -> str:
    text = CONFIG.format(
        max_body=1_000_000, max_json_body=1_000_000, concurrent=8, attempts=2, cooldown=5,
        models=MODELS,
    )  # fmt: skip
    (tmp_path / "spendgate.yaml").write_text(text.replace(SQLITE_BACKEND, ledger))
    (tmp_path / "catalog_override.yaml").write_text(CATALOG_OVERRIDE)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)

    async def no_listener(*_args: object) -> None:
        return None

    monkeypatch.setattr(serve, "_run", no_listener)  # `serve` starts and returns at once
    return str(tmp_path / "spendgate.yaml")


def test_serve_refuses_a_schema_that_is_behind_and_says_to_run_migrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, monkeypatch, "auto_migrate: false, " + SQLITE_BACKEND)
    settings = load(config, env=ENV)
    assert settings.ledger_path is not None
    monkeypatch.setattr(migrations, "load", lambda d: REAL_LOAD(d)[:-1])  # the previous release
    SqliteLedger.open(settings.ledger_path, currency="USD").close()
    monkeypatch.setattr(migrations, "load", REAL_LOAD)

    assert main(["serve", "--no-bootstrap", "--config", config]) == EXIT_UNAVAILABLE
    err = capsys.readouterr().err
    assert err.startswith("error: the ledger schema is at version")
    assert "run `spendgate migrate" in err and "Traceback" not in err
    # `--local` admin commands follow the same policy: they never migrate a shared ledger.
    assert main(["status", "--local", "--config", config]) == EXIT_UNAVAILABLE
    assert "spendgate migrate" in capsys.readouterr().err

    assert main(["serve", "--no-bootstrap", "--migrate", "--config", config]) == EXIT_OK
    assert main(["serve", "--no-bootstrap", "--config", config]) == EXIT_OK


def test_sqlite_without_auto_migrate_set_still_migrates_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_config(tmp_path, monkeypatch, SQLITE_BACKEND)
    assert main(["serve", "--no-bootstrap", "--config", config]) == EXIT_OK
    assert (tmp_path / "ledger.db").exists()


@pytest.mark.postgres
def test_postgres_serve_does_not_migrate_unless_asked_and_migrate_upgrades(
    postgres_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dsn = workdir_location("postgres", tmp_path / "db")
    monkeypatch.setenv("TEST_LEDGER_DSN", dsn)
    config = write_config(
        tmp_path, monkeypatch, 'backend: {kind: postgres, dsn: "env:TEST_LEDGER_DSN"}'
    )
    assert main(["serve", "--no-bootstrap", "--config", config]) == EXIT_UNAVAILABLE
    assert "run `spendgate migrate" in capsys.readouterr().err
    with psycopg.connect(dsn) as conn:
        found = conn.execute("SELECT to_regclass('schema_migrations')").fetchone()
    assert found == (None,)  # nothing was created by the refused start

    assert main(["migrate", "--config", config]) == EXIT_OK
    assert main(["serve", "--no-bootstrap", "--config", config]) == EXIT_OK
