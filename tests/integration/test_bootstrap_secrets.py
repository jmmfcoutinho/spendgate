"""ADR-0032: `migrate --bootstrap` hands each new secret to a terminal or a 0600 file, never a log.

T-LOG-04 for `migrate`. The terminal is injected (a stream whose `isatty()` says so); pytest's
captured stderr is not a terminal, like a Kubernetes Job's or a CI runner's.
"""

import asyncio
import io
import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from spendgate.cli import main, serve
from spendgate.cli.client import EXIT_OK, EXIT_USAGE
from spendgate.config import load
from spendgate.ledger.key_types import AdminScope, KeyKind, Principal
from spendgate.ledger.keys import KeyService
from spendgate.ledger.sqlite import SqliteLedger
from tests.integration.harness import (
    CATALOG_OVERRIDE,
    CONFIG,
    ENV,
    MODELS,
    NOW_MS,
    PEPPER,
    async_test,
)
from tests.integration.test_admin_bootstrap import BOOTSTRAP

SECRET = re.compile(r"\bsga?-[0-9a-f]{12}-[0-9a-f]{48}\b")
NO_KEYS = """\
bootstrap:
  scopes: ["org:acme/project:app"]
  budgets:
    - {id: monthly, scope: "org:acme/project:app", period: month, cap: "40 USD"}
"""


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str = BOOTSTRAP) -> str:
    text = CONFIG.format(
        max_body=1_000_000, max_json_body=1_000_000, concurrent=8, attempts=2, cooldown=5,
        models=MODELS,
    )  # fmt: skip
    (tmp_path / "spendgate.yaml").write_text(text + extra)
    (tmp_path / "catalog_override.yaml").write_text(CATALOG_OVERRIDE)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    return str(tmp_path / "spendgate.yaml")


def ledger_of(config: str) -> SqliteLedger:
    settings = load(config, env=ENV)
    assert settings.ledger_path is not None
    return SqliteLedger.open(settings.ledger_path, currency="USD")


def bootstrap_rows(config: str) -> tuple[int, bool, bool]:
    """(keys, the bootstrap scope exists, the bootstrap budget exists) in the config's ledger."""

    async def read() -> tuple[int, bool, bool]:
        ledger = ledger_of(config)
        try:
            api, admin = (
                await ledger.list_keys(None),
                await ledger.list_keys(None, kind=KeyKind.ADMIN),
            )
            keys = len(api) + len(admin)
            scope = await ledger.find_scope("org:acme/project:app")
            budget = await ledger.get_budget("bootstrap-monthly", NOW_MS)
            return keys, scope is not None, budget is not None
        finally:
            ledger.close()

    return asyncio.run(read())


def migrate(config: str, *extra: str) -> int:
    return main(["migrate", "--bootstrap", "--config", config, *extra])


# T-LOG-04
def test_t_log_04_migrate_bootstrap_prints_each_secret_once_to_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_config(tmp_path, monkeypatch)
    terminal = Terminal()
    monkeypatch.setattr(sys, "stderr", terminal)
    assert migrate(config) == EXIT_OK
    secrets = SECRET.findall(terminal.getvalue())
    assert len(secrets) == 2 and len(set(secrets)) == 2
    assert "bootstrap: created api key app-key (org:acme/project:app): sg-" in terminal.getvalue()
    again = Terminal()
    monkeypatch.setattr(sys, "stderr", again)
    assert migrate(config) == EXIT_OK
    assert not SECRET.search(again.getvalue())  # create-only: a secret is never shown twice


# T-LOG-04
def test_t_log_04_migrate_bootstrap_without_a_terminal_refuses_before_any_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, monkeypatch)
    assert migrate(config) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "--secrets-file" in err and "app-key (org:acme/project:app)" in err
    assert "ops (org:spendgate-admin)" in err and not SECRET.search(err)
    assert bootstrap_rows(config) == (0, False, False)  # no key whose secret nobody saw


def test_migrate_bootstrap_without_a_terminal_still_creates_scopes_and_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, monkeypatch, NO_KEYS)
    assert migrate(config) == EXIT_OK
    assert "bootstrap: created budget monthly" in capsys.readouterr().err
    assert bootstrap_rows(config) == (0, True, True)


# T-LOG-04
def test_t_log_04_a_rerun_without_a_terminal_passes_once_every_key_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Job that runs `migrate --bootstrap` on every deploy needs the file only the first time."""
    config = write_config(tmp_path, monkeypatch)
    assert migrate(config, "--secrets-file", str(tmp_path / "keys.jsonl")) == EXIT_OK
    capsys.readouterr()
    assert migrate(config) == EXIT_OK
    assert not SECRET.search(capsys.readouterr().err)
    assert bootstrap_rows(config) == (2, True, True)


# T-LOG-04
def test_t_log_04_secrets_file_is_0600_holds_each_secret_once_and_none_is_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, monkeypatch)
    out = tmp_path / "keys.jsonl"
    terminal = Terminal()
    monkeypatch.setattr(sys, "stderr", terminal)  # a terminal too: the file still wins
    previous = os.umask(0)  # the widest umask: the mode comes from the open, not from it
    try:
        assert migrate(config, "--secrets-file", str(out)) == EXIT_OK
    finally:
        os.umask(previous)
    err = terminal.getvalue()
    assert not SECRET.search(err) and not SECRET.search(capsys.readouterr().out)
    assert f"bootstrap: wrote 2 key secrets to {out}" in err
    assert "bootstrap: created api key app-key (org:acme/project:app)\n" in err
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    entries = [json.loads(line) for line in out.read_text().splitlines()]
    assert [(e["name"], e["kind"], e["scope"]) for e in entries] == [
        ("app-key", "api", "org:acme/project:app"),
        ("ops", "admin", "org:spendgate-admin"),
    ]
    asyncio.run(_verify(config, entries[0]["secret"], entries[1]["secret"]))


async def _verify(config: str, api: str, admin: str) -> None:
    ledger = ledger_of(config)
    try:
        keys = KeyService(ledger, PEPPER)
        assert isinstance(await keys.verify_api(api, NOW_MS), Principal)
        assert isinstance(await keys.verify_admin(admin, NOW_MS, AdminScope.KEYS_WRITE), Principal)
    finally:
        ledger.close()


# T-LOG-04
@pytest.mark.parametrize("existing", ["file", "symlink"])
def test_t_log_04_secrets_file_refuses_an_existing_path_before_any_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    existing: str,
) -> None:  # fmt: skip
    config = write_config(tmp_path, monkeypatch)
    out = tmp_path / "keys.jsonl"
    if existing == "file":
        out.write_text("an earlier run's keys\n")
    else:
        out.symlink_to(tmp_path / "elsewhere")  # dangling: O_EXCL never follows it
    assert migrate(config, "--secrets-file", str(out)) == EXIT_USAGE
    assert "exists; refusing to overwrite it" in capsys.readouterr().err
    assert bootstrap_rows(config) == (0, False, False)
    if existing == "file":
        assert out.read_text() == "an earlier run's keys\n"
    else:
        assert not (tmp_path / "elsewhere").exists()


def test_secrets_file_is_not_written_when_no_key_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, monkeypatch, NO_KEYS)
    out = tmp_path / "keys.jsonl"
    assert migrate(config, "--secrets-file", str(out)) == EXIT_OK
    assert f"bootstrap: no key to create; {out} not written" in capsys.readouterr().err
    assert not out.exists()


def test_secrets_file_needs_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, monkeypatch)
    out = str(tmp_path / "keys.jsonl")
    assert main(["migrate", "--secrets-file", out, "--config", config]) == EXIT_USAGE
    assert "--secrets-file needs --bootstrap" in capsys.readouterr().err


@async_test
async def test_a_failure_after_the_first_key_keeps_its_secret_in_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each secret is synced to the file as its key is created, not when the run ends."""
    config = write_config(tmp_path, monkeypatch)
    real, made = KeyService.create_key, list[int]()

    async def second_fails(self: KeyService, *args: object, **kwargs: object) -> object:
        if made:
            raise RuntimeError("the ledger went away")
        made.append(1)
        return await real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(KeyService, "create_key", second_fails)
    out = tmp_path / "keys.jsonl"
    with pytest.raises(RuntimeError):
        await serve._migrate(
            load(config, env=ENV), bootstrap=True, secrets_file=str(out), interactive=False
        )
    (entry,) = [json.loads(line) for line in out.read_text().splitlines()]
    assert entry["name"] == "app-key" and SECRET.fullmatch(entry["secret"])
