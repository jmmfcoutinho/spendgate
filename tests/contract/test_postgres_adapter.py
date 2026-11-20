"""Postgres-specific behaviour: retry on conflict, error mapping, migrations, currency."""

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest

from spendgate.core.budgets import Period, ScopeKind
from spendgate.ledger import migrations, sql
from spendgate.ledger.common import i
from spendgate.ledger.port import ConflictError, CurrencyMismatch, Held, LedgerUnavailable
from spendgate.ledger.postgres import PostgresLedger
from spendgate.ledger.postgres_driver import MAX_RETRIES
from spendgate.ledger.sql import Flow, Stmt, q
from tests.contract.rig import CURRENCY, E, Rig, workdir_location

pytestmark = pytest.mark.postgres

DEADLOCK = "DO $$ BEGIN RAISE EXCEPTION 'injected' USING ERRCODE = '40P01'; END $$"
SERIALIZATION = "DO $$ BEGIN RAISE EXCEPTION 'injected' USING ERRCODE = '40001'; END $$"


@pytest.fixture
def pg(postgres_url: str, tmp_path: Path, new_id: Callable[[], str]) -> Iterator[Rig]:
    made = Rig("postgres", workdir_location("postgres", tmp_path), new_id)
    yield made
    made.close()


def pg_ledger(rig: Rig) -> PostgresLedger:
    assert isinstance(rig.ledger, PostgresLedger)
    return rig.ledger


def flaky(statement: str, failures: int, attempts: list[int]) -> Flow[int]:
    """A flow that dies with a server-side error on its first `failures` runs."""
    attempts.append(1)
    if len(attempts) <= failures:
        yield Stmt(statement, {})
    rows = yield q("SELECT 7 AS n")
    return i(rows[0], "n")


@pytest.mark.parametrize("statement", [DEADLOCK, SERIALIZATION])
def test_a_conflict_reruns_the_whole_flow_and_succeeds(pg: Rig, statement: str) -> None:
    attempts: list[int] = []
    ledger = pg_ledger(pg)
    assert pg.run(ledger._run(lambda: flaky(statement, 2, attempts))) == 7
    assert len(attempts) == 3  # a fresh flow each time, from the factory
    assert ledger.stats.total == 2
    assert ledger.stats.exhausted == 0


def test_after_three_retries_the_ledger_is_unavailable(pg: Rig) -> None:
    attempts: list[int] = []
    ledger = pg_ledger(pg)
    with pytest.raises(LedgerUnavailable, match="conflicted 4 times"):
        pg.run(ledger._run(lambda: flaky(DEADLOCK, 99, attempts)))
    assert len(attempts) == MAX_RETRIES + 1
    assert ledger.stats.exhausted == 1
    assert pg.run(ledger._run(lambda: flaky(DEADLOCK, 0, []))) == 7


def test_another_server_error_is_not_retried(pg: Rig) -> None:
    attempts: list[int] = []
    ledger = pg_ledger(pg)
    syntax = "SELEKT 1"
    with pytest.raises(LedgerUnavailable):
        pg.run(ledger._run(lambda: flaky(syntax, 99, attempts)))
    assert len(attempts) == 1
    assert ledger.stats.total == 0


def crossing(first: str, second: str, ran: list[str]) -> Flow[int]:
    """Lock two budgets in a given order with a pause between: opposite orders deadlock."""
    ran.append(first)
    yield q(sql.BUDGET_LOCK, id=first)
    yield q("SELECT pg_sleep(0.4)")
    yield q(sql.BUDGET_LOCK, id=second)
    return 1


def test_a_real_deadlock_is_detected_retried_and_both_sides_finish(pg: Rig) -> None:
    w = pg.world("100", org_cap="100")
    a, b = w.budget.id, w.extra[0].id
    ledger, ran = pg_ledger(pg), list[str]()

    async def both() -> tuple[int, int]:
        return await asyncio.gather(
            ledger._run(lambda: crossing(a, b, ran)),
            ledger._run(lambda: crossing(b, a, ran)),
        )

    assert list(pg.run(both())) == [1, 1]
    assert ledger.stats.by_sqlstate["40P01"] >= 1  # the server picked a victim; it re-ran
    assert len(ran) >= 3


def test_two_holds_with_one_request_id_one_wins_the_other_conflicts(pg: Rig) -> None:
    w = pg.world("100")

    async def race() -> list[object]:
        calls = [pg.ledger.hold(pg.request(w, "same", "1", instance=n)) for n in "AB"]
        return list(await asyncio.gather(*calls, return_exceptions=True))

    results = pg.run(race())
    assert sum(isinstance(r, Held) for r in results) == 1
    assert sum(isinstance(r, ConflictError) for r in results) == 1


def test_an_unreachable_server_is_ledger_unavailable() -> None:
    with pytest.raises(LedgerUnavailable):
        asyncio.run(PostgresLedger.open("postgresql://nobody@127.0.0.1:1/none?connect_timeout=2"))


def backend_pid() -> Flow[int]:
    rows = yield q("SELECT pg_backend_pid() AS pid")
    return i(rows[0], "pid")


def test_a_lost_connection_mid_use_is_ledger_unavailable_then_recovers(pg: Rig) -> None:
    w = pg.world("100")
    ledger = pg_ledger(pg)
    pg.hold_ok(w, "r1", "1")
    pid = pg.run(ledger._run(backend_pid))
    with psycopg.connect(pg.location, autocommit=True) as admin:
        # Only this ledger's own backend: other tests share the server.
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    outcomes: list[object] = []
    for n in range(4):  # the pool detects the dead connection and reconnects
        try:
            outcomes.append(pg.hold(w, f"later-{n}", "1"))
        except LedgerUnavailable as exc:
            outcomes.append(exc)
    assert isinstance(outcomes[-1], Held), outcomes
    assert ledger.stats.exhausted == 0


# --- migrations and currency -------------------------------------------------------------------


def raw(rig: Rig) -> psycopg.Connection:
    return psycopg.connect(rig.location, autocommit=True)


def test_migrations_are_recorded_with_checksums(pg: Rig) -> None:
    with raw(pg) as conn:
        rows = conn.execute("SELECT version, checksum FROM schema_migrations ORDER BY 1").fetchall()
    assert rows == [(m.version, m.checksum) for m in migrations.load("postgres")]


def test_a_changed_applied_migration_is_refused(pg: Rig) -> None:
    pg.close_connection()
    with raw(pg) as conn:
        conn.execute("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1")
    with pytest.raises(LedgerUnavailable, match="changed after it was applied"):
        asyncio.run(PostgresLedger.open(pg.location, currency=CURRENCY))


def test_a_ledger_migrated_by_a_newer_version_is_refused(pg: Rig) -> None:
    pg.close_connection()
    with raw(pg) as conn:
        conn.execute("INSERT INTO schema_migrations VALUES (999, 'future', 'x', 0)")
    with pytest.raises(LedgerUnavailable, match="newer spendgate"):
        asyncio.run(PostgresLedger.open(pg.location, currency=CURRENCY))


def test_the_ledger_currency_is_persisted_and_a_different_one_refused(pg: Rig) -> None:
    pg.close_connection()
    with raw(pg) as conn:
        stored = conn.execute("SELECT value FROM ledger_meta WHERE key = 'currency'").fetchone()
    assert stored == (CURRENCY,)
    with pytest.raises(CurrencyMismatch, match=r"EUR.*USD"):
        asyncio.run(PostgresLedger.open(pg.location, currency="USD"))


def test_opening_twice_in_parallel_migrates_once(postgres_url: str, tmp_path: Path) -> None:
    location = workdir_location("postgres", tmp_path)

    async def open_two() -> None:
        ledgers = await asyncio.gather(
            *(PostgresLedger.open(location, currency=CURRENCY) for _ in range(4))
        )
        assert len(ledgers) == 4

    asyncio.run(open_two())
    with psycopg.connect(location) as conn:
        count = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()
    assert count == (len(migrations.load("postgres")),)


def test_money_columns_are_bigint_and_cap_survives_a_large_value(pg: Rig) -> None:
    scope = pg.run(pg.ledger.create_scope(ScopeKind.ORG, "big", None, now=0))
    huge = E("9000000000")  # 9e18 nano-units: near the BIGINT limit
    budget = pg.run(pg.ledger.create_budget(scope.id, Period.NONE, huge, now=0))
    assert budget.cap_nano == huge
