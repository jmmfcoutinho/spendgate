"""Library-mode parts that need no pipeline: principal, session limits, loop thread, bootstrap."""

import asyncio
import threading
from pathlib import Path

import pytest

from spendgate.client.session import SessionContext, activate_session
from spendgate.core.errors import Refusal
from spendgate.core.money import Money
from spendgate.core.sessions import SessionDefaults
from spendgate.ledger.sqlite import SqliteLedger
from spendgate.local import CapMismatch, LocalPrincipal, NoBudget, default_ledger_path
from spendgate.local.loop import LoopThread
from spendgate.local.principal import session_defaults
from spendgate.local.setup import prepare_scope

NOW = 1_794_000_000_000


def test_session_defaults_carry_limits_in_ledger_units() -> None:
    ctx = SessionContext("s", "2 USD", 200, 90.5, 30)
    got = session_defaults(ctx, "USD")
    assert got == SessionDefaults(
        budget_nano=Money.from_amount("2", "USD").nano, max_steps=200, max_seconds=91,
        idle_seconds=30,
    )  # fmt: skip


def test_a_session_without_limits_still_gets_empty_defaults() -> None:
    """The ledger refuses a session header whose key has no `session_defaults`."""
    assert session_defaults(SessionContext("s"), "USD") == SessionDefaults()
    assert session_defaults(None, "USD") == SessionDefaults()


def test_a_session_budget_in_another_currency_is_an_error() -> None:
    with pytest.raises(ValueError, match="ledger currency USD"):
        session_defaults(SessionContext("s", "2 EUR"), "USD")


def test_the_local_principal_accepts_any_credential_and_reads_the_active_session() -> None:
    principal = LocalPrincipal("scope-1", "USD", max_output_tokens=500)
    plain = asyncio.run(principal.verify_api("anything", NOW))
    assert not isinstance(plain, Refusal)
    assert plain.scope_id == "scope-1" and plain.policy.max_output_tokens == 500
    assert plain.policy.session_defaults == SessionDefaults()
    with activate_session(SessionContext("s", max_steps=3)):
        limited = asyncio.run(principal.verify_api("anything", NOW))
    assert not isinstance(limited, Refusal)
    assert limited.policy.session_defaults == SessionDefaults(max_steps=3)


def test_loop_thread_runs_coroutines_off_the_callers_thread_and_even_inside_a_loop() -> None:
    loop = LoopThread()

    async def where() -> int | None:
        return threading.current_thread().ident

    async def inside_a_running_loop() -> int | None:
        return loop.run(where())  # the notebook case

    try:
        assert loop.run(where()) != threading.current_thread().ident
        assert asyncio.run(inside_a_running_loop()) != threading.current_thread().ident
    finally:
        loop.close()
        loop.close()  # twice is fine


def test_loop_thread_reraises_the_coroutines_error() -> None:
    loop = LoopThread()

    async def boom() -> None:
        raise KeyError("x")

    try:
        with pytest.raises(KeyError):
            loop.run(boom())
    finally:
        loop.close()


def test_default_ledger_path_follows_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_ledger_path() == tmp_path / "spendgate" / "local-ledger.db"


def test_prepare_scope_is_idempotent_and_never_changes_a_cap(tmp_path: Path) -> None:
    path = tmp_path / "l.db"
    ledger = SqliteLedger.open(path, currency="USD")
    five = Money.from_amount("5", "USD")

    async def run() -> tuple[str, str]:
        first = await prepare_scope(ledger, path, five, NOW)
        again = await prepare_scope(ledger, path, five, NOW)
        reuse = await prepare_scope(ledger, path, None, NOW)  # reuse without naming the cap
        assert reuse == first
        with pytest.raises(CapMismatch):
            await prepare_scope(ledger, path, Money.from_amount("6", "USD"), NOW)
        return first, again

    try:
        first, again = asyncio.run(run())
        rows = asyncio.run(ledger.totals(first, NOW))
    finally:
        ledger.close()
    assert first == again
    assert [r.effective_cap_nano for r in rows] == [five.nano]  # one budget, not two


def test_prepare_scope_without_any_budget_refuses(tmp_path: Path) -> None:
    path = tmp_path / "l.db"
    ledger = SqliteLedger.open(path, currency="USD")
    try:
        with pytest.raises(NoBudget):
            asyncio.run(prepare_scope(ledger, path, None, NOW))
    finally:
        ledger.close()
