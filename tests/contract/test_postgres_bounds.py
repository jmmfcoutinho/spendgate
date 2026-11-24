"""Postgres session bounds: a locked row or a stuck statement ends in `LedgerUnavailable`, fast.

Every pooled connection gets `lock_timeout`, `statement_timeout`,
`idle_in_transaction_session_timeout` and TCP keepalives, unless the operator set them already.
"""

import asyncio
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import errors
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from spendgate.ledger import sql
from spendgate.ledger.common import i
from spendgate.ledger.port import Held, LedgerUnavailable
from spendgate.ledger.postgres import (
    SESSION_DEFAULTS,
    TCP_DEFAULTS,
    WAITING_PER_CONNECTION,
    PostgresLedger,
    tcp_options_for,
)
from spendgate.ledger.sql import Flow, q
from tests.contract.rig import CURRENCY, T0, Rig, workdir_location

pytestmark = pytest.mark.postgres

_GUARD_S = 15  # a test that would wait forever without the bounds fails instead of hanging


@pytest.fixture
def pg(postgres_url: str, tmp_path: Path, new_id: Callable[[], str]) -> Iterator[Rig]:
    made = Rig("postgres", workdir_location("postgres", tmp_path), new_id)
    yield made
    made.close()


def pg_ledger(rig: Rig) -> PostgresLedger:
    assert isinstance(rig.ledger, PostgresLedger)
    return rig.ledger


def show(names: tuple[str, ...]) -> Callable[[], Flow[dict[str, str]]]:
    def flow() -> Flow[dict[str, str]]:
        found: dict[str, str] = {}
        for name in names:
            rows = yield q("SELECT current_setting(:name) AS v", name=name)
            found[name] = str(rows[0]["v"])
        return found

    return flow


async def pooled_parameters(ledger: PostgresLedger) -> dict[str, str]:
    """The libpq options a pooled connection was opened with."""
    async with (await ledger._ensure_pool()).connection() as conn:
        return conn.info.get_parameters()


def overriding(location: str, **libpq: str) -> str:
    """The same schema DSN with operator choices: a lower lock and statement timeout, own idle."""
    options = conninfo_to_dict(location)["options"]
    extra = " -c lock_timeout=300ms -c statement_timeout=300ms"
    return make_conninfo(location, options=f"{options}{extra}", **libpq)


def test_pooled_connections_get_the_session_bounds_and_tcp_options(pg: Rig) -> None:
    ledger = pg_ledger(pg)
    assert pg.run(ledger._run(show(tuple(SESSION_DEFAULTS)))) == SESSION_DEFAULTS
    parameters = pg.run(pooled_parameters(ledger))
    assert {k: parameters.get(k) for k in TCP_DEFAULTS} == TCP_DEFAULTS


def test_bounds_and_tcp_options_the_dsn_sets_win(pg: Rig) -> None:
    dsn = overriding(pg.location, keepalives_idle="7")
    assert "keepalives_idle" not in tcp_options_for(dsn)
    other = pg.run(PostgresLedger.open(dsn, currency=CURRENCY))
    try:
        assert pg.run(other._run(show(tuple(SESSION_DEFAULTS)))) == {
            "lock_timeout": "300ms",
            "statement_timeout": "300ms",
            "idle_in_transaction_session_timeout": "10s",  # not in the DSN: the default applies
        }
        parameters = pg.run(pooled_parameters(other))
        assert parameters["keepalives_idle"] == "7"
        assert parameters["keepalives"] == "1"
    finally:
        pg.run(other.aclose())


def test_m12_a_locked_budget_row_is_ledger_unavailable_within_the_lock_timeout(pg: Rig) -> None:
    """The row another session holds is waited on for `lock_timeout`, not retried, nothing held."""
    w = pg.world("100")
    ledger = pg_ledger(pg)
    request = pg.request(w, "locked", "1")
    with psycopg.connect(pg.location) as locker:  # one open transaction holds the row lock
        locker.execute(sql.render(sql.BUDGET_LOCK, sql.POSTGRES), {"id": w.budget.id})
        started = time.monotonic()
        with pytest.raises(LedgerUnavailable) as caught:
            pg.run(asyncio.wait_for(ledger.hold(request), _GUARD_S))
        waited = time.monotonic() - started
        locker.rollback()
    assert isinstance(caught.value.__cause__, errors.LockNotAvailable)  # 55P03
    assert 1.5 <= waited < 5  # about lock_timeout (2 s): bounded, and no retry piled on top
    assert ledger.stats.total == 0
    assert pg.run(ledger.get_reservation("locked")) is None  # rolled back: no pending hold
    assert isinstance(pg.hold(w, "after", "1"), Held)  # the lock is gone: the ledger works


def sleeping(seconds: int, attempts: list[int]) -> Flow[int]:
    attempts.append(1)
    rows = yield q("SELECT 1 AS n FROM pg_sleep(:seconds)", seconds=seconds)
    return i(rows[0], "n")


def test_m12_a_statement_over_the_timeout_is_ledger_unavailable_and_not_retried(pg: Rig) -> None:
    other = pg.run(PostgresLedger.open(overriding(pg.location), currency=CURRENCY))
    attempts: list[int] = []
    try:
        with pytest.raises(LedgerUnavailable) as caught:
            pg.run(asyncio.wait_for(other._run(lambda: sleeping(10, attempts)), _GUARD_S))
    finally:
        pg.run(other.aclose())
    assert isinstance(caught.value.__cause__, errors.QueryCanceled)  # 57014
    assert attempts == [1]
    assert other.stats.total == 0


def test_renewal_skips_a_row_another_session_has_locked_and_renews_the_rest(pg: Rig) -> None:
    """A hold being settled elsewhere (its row locked) must not fail the whole heartbeat."""
    w = pg.world("100")
    for rid in ("busy", "free-1", "free-2"):
        pg.hold_ok(w, rid, "1")
    ledger = pg_ledger(pg)
    with psycopg.connect(pg.location) as locker:
        locker.execute("SELECT id FROM reservations WHERE id = 'busy' FOR UPDATE")
        started = time.monotonic()
        renewed = pg.run(
            asyncio.wait_for(ledger.renew_all("A", {"busy", "free-1", "free-2"}, T0, 60_000), 15)
        )
        waited = time.monotonic() - started
        locker.rollback()
    assert renewed == ("free-1", "free-2")  # no error: the heartbeat stops no stream
    assert waited < 1  # skipped, not waited for up to lock_timeout
    assert pg.run(ledger.renew_all("A", {"busy"}, T0, 60_000)) == ("busy",)  # next beat


def test_reports_and_retention_get_a_longer_statement_timeout_unless_the_operator_set_one(
    pg: Rig,
) -> None:
    ledger = pg_ledger(pg)
    names = ("statement_timeout",)
    assert pg.run(ledger._run(show(names), slow=True)) == {"statement_timeout": "1min"}
    assert pg.run(ledger._run(show(names))) == {"statement_timeout": "5s"}  # SET LOCAL ended
    other = pg.run(PostgresLedger.open(overriding(pg.location), currency=CURRENCY))
    try:
        assert pg.run(other._run(show(names), slow=True)) == {"statement_timeout": "300ms"}
    finally:
        pg.run(other.aclose())


def test_callers_past_the_pool_waiting_limit_are_ledger_unavailable_at_once(pg: Rig) -> None:
    small = pg.run(PostgresLedger.open(pg.location, currency=CURRENCY, max_size=1))
    names = ("lock_timeout",)

    async def stall() -> None:
        pool = await small._ensure_pool()
        async with pool.connection():  # the only connection is busy: a stalled server
            waiting = [asyncio.ensure_future(small._run(show(names))) for _ in range(2)]
            for _ in range(200):  # until both queue for the connection
                if pool.get_stats().get("requests_waiting", 0) == WAITING_PER_CONNECTION:
                    break
                await asyncio.sleep(0.005)
            started = time.monotonic()
            with pytest.raises(LedgerUnavailable):
                await small._run(show(names))
            assert time.monotonic() - started < 1  # refused, not queued for the pool timeout
        assert len(await asyncio.gather(*waiting)) == 2  # the queued callers still finish

    try:
        pg.run(stall())
    finally:
        pg.run(small.aclose())
