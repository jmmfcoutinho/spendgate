"""SIGHUP: `file:` provider credentials and the ledger DSN are re-read in a running gateway.

The handler is called directly (no signal is sent) and the test waits for the reload task it
schedules; the fake upstream shows which key went out.
"""

import asyncio
import io
import json
import logging
import signal
import threading
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from spendgate.cli.reload import SecretReload, install, on_sighup
from spendgate.config import Reread, Secret, Settings, load, reread_file_secrets
from spendgate.config.schema import PostgresLedger as PostgresBackend
from spendgate.gateway.logs import configure_json_logging
from spendgate.ledger.postgres import PostgresLedger
from spendgate.pipeline.pipeline import Pipeline
from tests.integration.harness import Stack, async_test, open_stack
from tests.unit.test_config import ENV, write

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


def with_dsn_file(s: Stack, dsn_file: Path, old_dsn: Secret) -> Settings:
    """The stack's settings with a Postgres ledger whose `file:` DSN was read as `old_dsn`."""
    backend = PostgresBackend(kind="postgres", dsn=f"file:{dsn_file}")
    ledger = s.settings.config.ledger.model_copy(update={"backend": backend})
    config = s.settings.config.model_copy(update={"ledger": ledger})
    return replace(s.settings, config=config, ledger_dsn=old_dsn)


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
        await reload.wait()
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
        reload = SecretReload(s.settings, s.pipeline, logger)
        reload()
        await reload.wait()
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
        logger, out = capture()
        reload = SecretReload(with_dsn_file(s, dsn_file, old_dsn), s.pipeline, logger, rotate)
        reload()
        await reload.wait()
        assert len(rotated) == 1 and reload.settings.ledger_dsn == old_dsn
        failed = [e for e in events(out) if e["event"] == "secret_reload_failed"]
        assert failed[0]["where"] == "ledger.backend.dsn"
        assert events(out)[-1]["changed"] == ""
        assert "new@" not in out.getvalue() and "old@" not in out.getvalue()


@async_test
async def test_a_new_dsn_password_reaches_the_ledger(tmp_path: Path) -> None:
    old_dsn = Secret("postgresql://u:old-pw@db/spend")  # pragma: allowlist secret
    new_dsn = "postgresql://u:new-pw@db/spend"  # pragma: allowlist secret
    dsn_file = tmp_path / "dsn"
    dsn_file.write_text(new_dsn)
    pg = PostgresLedger(old_dsn.reveal(), "USD", lambda: "id")  # never connects here
    async with open_stack(tmp_path) as s:
        logger, out = capture()
        reload = SecretReload(
            with_dsn_file(s, dsn_file, old_dsn), s.pipeline, logger, pg.rotate_dsn
        )
        reload()
        await reload.wait()
        assert reload.settings.ledger_dsn == Secret(new_dsn)
        assert pg.rotate_dsn(new_dsn) is False  # the ledger already holds the new DSN
        assert events(out)[-1]["changed"] == "ledger.backend.dsn"
        assert "old-pw" not in out.getvalue() and "new-pw" not in out.getvalue()


@async_test
async def test_a_malformed_dsn_file_is_refused_and_provider_keys_still_rotate(
    tmp_path: Path,
) -> None:
    key, dsn_file = tmp_path / "openai_key", tmp_path / "dsn"
    key.write_text(OLD)
    old_dsn = Secret("postgresql://u:old-pw@db/spend")  # pragma: allowlist secret
    pg = PostgresLedger(old_dsn.reveal(), "USD", lambda: "id")
    async with open_stack(tmp_path, models=models(key)) as s:
        logger, out = capture()
        settings = with_dsn_file(s, dsn_file, old_dsn)
        reload = SecretReload(settings, s.pipeline, logger, pg.rotate_dsn)
        key.write_text(NEW)
        dsn_file.write_text("postgresql://u:pw-fragment@[db/spend")  # pragma: allowlist secret
        reload()
        await reload.wait()
        assert (await s.chat()).status_code == 200
        assert s.fake.requests[-1].headers["authorization"] == f"Bearer {NEW}"
        assert reload.settings.ledger_dsn == old_dsn
        failed = [e for e in events(out) if e["event"] == "secret_reload_failed"]
        assert [e["where"] for e in failed] == ["ledger.backend.dsn"]
        assert "not a valid connection string" in str(failed[0]["reason"])
        assert events(out)[-1]["changed"] == "models.alpha.deployments.0.credential"
        assert "pw-fragment" not in out.getvalue() and NEW not in out.getvalue()


@async_test
async def test_the_loop_keeps_serving_while_a_reload_blocks_and_signals_coalesce(
    tmp_path: Path,
) -> None:
    key = tmp_path / "openai_key"
    key.write_text(OLD)
    started, gate, reads = threading.Event(), threading.Event(), [0]

    def slow_read(settings: Settings) -> Reread:  # a hung mount: blocks its worker thread
        reads[0] += 1
        started.set()
        assert gate.wait(timeout=10), "the test never released the reader"
        return reread_file_secrets(settings)

    async with open_stack(tmp_path, models=models(key)) as s:
        logger, _ = capture()
        reload = SecretReload(s.settings, s.pipeline, logger, read=slow_read)
        try:
            reload()
            assert await asyncio.to_thread(started.wait, 10)  # the first read is under way
            key.write_text(NEW)
            reload()  # during the reload: one more read after it, not one per signal
            reload()
            assert (await s.chat()).status_code == 200  # served while the read is stuck
            assert s.fake.requests[0].headers["authorization"] == f"Bearer {OLD}"
        finally:
            gate.set()
        await reload.wait()
        assert reads[0] == 2
        assert reload.settings.routes["alpha"].deployments[0].credential == Secret(NEW)


@async_test
async def test_a_handler_failure_is_logged_never_raised(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        logger, out = capture()
        broken = replace(s.settings, routes={})  # routes no longer match the config
        reload = SecretReload(broken, s.pipeline, logger)
        reload()
        await reload.wait()
        assert events(out)[-1]["event"] == "secret_reload_failed"
        assert events(out)[-1]["error_class"] == "KeyError"


@async_test
async def test_install_registers_the_handler_for_sighup() -> None:
    loop = asyncio.get_running_loop()
    calls: list[int] = []
    if not install(loop, lambda: calls.append(1)):  # pragma: no cover - POSIX in CI
        pytest.skip("no SIGHUP on this platform")
    assert loop.remove_signal_handler(signal.SIGHUP) is True


def test_shutdown_does_not_wait_for_a_reader_stuck_on_its_files(tmp_path: Path) -> None:
    started, gate, finished = threading.Event(), threading.Event(), threading.Event()
    settings = load(write(tmp_path), env=ENV)

    def stuck(settings: Settings) -> Reread:  # a hung mount
        started.set()
        gate.wait(timeout=30)
        finished.set()
        return reread_file_secrets(settings)

    async def serve_then_stop() -> bool:
        loop = asyncio.get_running_loop()
        never_applied = cast("Pipeline", None)  # the stuck read never returns a result to apply
        reload = SecretReload(settings, never_applied, logging.getLogger("test"), read=stuck)
        with on_sighup(loop, reload):  # the server's lifetime
            reload()
            assert await asyncio.to_thread(started.wait, 10)
        reload()  # after shutdown began: ignored
        return loop.remove_signal_handler(signal.SIGHUP)

    try:
        still_installed = asyncio.run(serve_then_stop())
        # `asyncio.run` returned while the read was still stuck: shutdown did not wait for it.
        assert not finished.is_set() and not still_installed
    finally:
        gate.set()
