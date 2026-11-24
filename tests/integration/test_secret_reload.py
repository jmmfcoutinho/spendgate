"""SIGHUP: `file:` provider credentials and the ledger DSN are re-read in a running gateway.

The handler is called directly (no signal is sent); the fake upstream shows which key went out.
"""

import asyncio
import io
import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from spendgate.cli.reload import SecretReload, install
from spendgate.config import Secret
from spendgate.config.schema import PostgresLedger as PostgresBackend
from spendgate.gateway.logs import configure_json_logging
from tests.integration.harness import async_test, open_stack

OLD = "sk-test-rotated-old-0000"  # pragma: allowlist secret
NEW = "sk-test-rotated-new-0000"  # pragma: allowlist secret


def models(key: Path) -> str:
    return f"""\
models:
  alpha:
    catalog_id: openai/gpt-6-luna
    deployments:
      - {{provider: openai, base_url: "https://a.upstream.test", credential: "file:{key}"}}
      - {{provider: openai, base_url: "https://b.upstream.test"}}
"""


def capture() -> tuple[logging.Logger, io.StringIO]:
    out = io.StringIO()
    return configure_json_logging(logging.StreamHandler(out)), out


def events(out: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in out.getvalue().splitlines()]


@async_test
async def test_sighup_swaps_a_rotated_provider_key_for_the_next_call(tmp_path: Path) -> None:
    key = tmp_path / "openai_key"
    key.write_text(OLD)
    async with open_stack(tmp_path, models=models(key)) as s:
        logger, out = capture()
        reload = SecretReload(s.settings, s.pipeline, logger)
        assert (await s.chat()).status_code == 200
        key.write_text(NEW + "\n")
        reload()
        assert (await s.chat()).status_code == 200
        sent = [r.headers["authorization"] for r in s.fake.requests]
        assert sent == [f"Bearer {OLD}", f"Bearer {NEW}"]
        assert reload.settings.routes["alpha"].deployments[0].credential == Secret(NEW)
        logged = events(out)
        assert logged[-1]["event"] == "secrets_reloaded"
        where = "models.alpha.deployments.0.credential"
        assert logged[-1]["reloaded"] == logged[-1]["changed"] == where
        assert OLD not in out.getvalue() and NEW not in out.getvalue()


@async_test
async def test_an_unreadable_file_keeps_the_old_key_and_is_logged(tmp_path: Path) -> None:
    key = tmp_path / "openai_key"
    key.write_text(OLD)
    async with open_stack(tmp_path, models=models(key)) as s:
        logger, out = capture()
        key.unlink()
        SecretReload(s.settings, s.pipeline, logger)()
        assert (await s.chat()).status_code == 200
        assert s.fake.requests[0].headers["authorization"] == f"Bearer {OLD}"
        failed = [e for e in events(out) if e["event"] == "secret_reload_failed"]
        assert failed[0]["where"] == "models.alpha.deployments.0.credential"
        assert failed[0]["level"] == "WARNING"


@async_test
async def test_a_dsn_that_moves_the_ledger_is_refused_and_the_old_one_kept(tmp_path: Path) -> None:
    dsn_file = tmp_path / "dsn"
    dsn_file.write_text("postgresql://u:new@elsewhere/spend")  # pragma: allowlist secret
    old_dsn = Secret("postgresql://u:old@db/spend")  # pragma: allowlist secret
    rotated: list[str] = []

    def rotate(dsn: str) -> bool:
        rotated.append(dsn)
        raise ValueError("the DSN changed more than its password; restart to apply it")

    async with open_stack(tmp_path) as s:
        backend = PostgresBackend(kind="postgres", dsn=f"file:{dsn_file}")
        ledger = s.settings.config.ledger.model_copy(update={"backend": backend})
        config = s.settings.config.model_copy(update={"ledger": ledger})
        settings = replace(s.settings, config=config, ledger_dsn=old_dsn)
        logger, out = capture()
        reload = SecretReload(settings, s.pipeline, logger, rotate)
        reload()
        assert len(rotated) == 1 and reload.settings.ledger_dsn == old_dsn
        failed = [e for e in events(out) if e["event"] == "secret_reload_failed"]
        assert failed[0]["where"] == "ledger.backend.dsn"
        assert events(out)[-1]["changed"] == ""
        assert "new@" not in out.getvalue() and "old@" not in out.getvalue()


@async_test
async def test_a_handler_failure_is_logged_never_raised(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        logger, out = capture()
        broken = replace(s.settings, routes={})  # routes no longer match the config
        SecretReload(broken, s.pipeline, logger)()
        assert events(out)[-1]["event"] == "secret_reload_failed"
        assert events(out)[-1]["error_class"] == "KeyError"


@async_test
async def test_install_registers_the_handler_for_sighup() -> None:
    loop = asyncio.get_running_loop()
    calls: list[int] = []
    if not install(loop, lambda: calls.append(1)):  # pragma: no cover - POSIX in CI
        pytest.skip("no SIGHUP on this platform")
    import signal  # noqa: PLC0415 - SIGHUP exists only on POSIX

    assert loop.remove_signal_handler(signal.SIGHUP) is True
