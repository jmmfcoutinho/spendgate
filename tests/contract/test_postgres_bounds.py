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
from spendgate.ledger.postgres import KEEPALIVES, SESSION_DEFAULTS, PostgresLedger, keepalives_for
from spendgate.ledger.sql import Flow, q
from tests.contract.rig import CURRENCY, Rig, workdir_location

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


def test_pooled_connections_get_the_session_bounds_and_keepalives(pg: Rig) -> None:
    ledger = pg_ledger(pg)
    assert pg.run(ledger._run(show(tuple(SESSION_DEFAULTS)))) == SESSION_DEFAULTS
    parameters = pg.run(pooled_parameters(ledger))
    assert {k: parameters.get(k) for k in KEEPALIVES} == KEEPALIVES


def test_bounds_and_keepalives_the_dsn_sets_win(pg: Rig) -> None:
    dsn = overriding(pg.location, keepalives_idle="7")
    assert "keepalives_idle" not in keepalives_for(dsn)
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
