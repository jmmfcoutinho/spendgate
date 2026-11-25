"""The Postgres driver's control flow, without a database.

`tests/contract/test_postgres_round_trips.py` checks the same driver against a real server; these
run in every `make test` (no `SPENDGATE_TEST_POSTGRES_URL`), with a fake connection that records
what is sent and a fake pool that hands it out.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Generator, Iterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, cast

import psycopg
import pytest
from psycopg import errors

from spendgate.ledger import postgres_driver
from spendgate.ledger.port import LedgerUnavailable
from spendgate.ledger.postgres_driver import (
    MAX_RETRIES,
    ConnectionDropped,
    Pool,
    RetryStats,
    run_flow,
    run_with_retry,
)
from spendgate.ledger.sql import Flow, Row, Tx, batch, finish, q

IDLE = psycopg.pq.TransactionStatus.IDLE
INTRANS = psycopg.pq.TransactionStatus.INTRANS


class FakeCursor:
    def __init__(self, rows: list[Row] | None) -> None:
        self.description = None if rows is None else ("col",)
        self._rows = rows or []

    async def fetchall(self) -> list[Row]:
        return self._rows


class FakeInfo:
    def __init__(self) -> None:
        self.transaction_status = IDLE


class FakeConn:
    """Records each statement and each pipeline (one round trip) it was sent in."""

    def __init__(self, rows: Callable[[str], list[Row] | None] = lambda _s: None) -> None:
        self.sent: list[str] = []
        self.round_trips = 0
        self.rolled_back = False
        self.broken = False
        self.info = FakeInfo()
        self.fail_on: str | None = None
        self.failure: BaseException = RuntimeError("boom")
        self._rows = rows
        self._in_pipeline = False

    async def execute(self, text: str, params: Mapping[str, object] | None = None) -> FakeCursor:
        if not self._in_pipeline:
            self.round_trips += 1
        self.sent.append(text)
        verb = text.split(maxsplit=1)[0]
        if self.fail_on is not None and verb == self.fail_on:
            raise self.failure
        if text.startswith("BEGIN"):
            self.info.transaction_status = INTRANS
        elif text in ("COMMIT", "ROLLBACK"):
            self.info.transaction_status = IDLE
        return FakeCursor(self._rows(text))

    @asynccontextmanager
    async def pipeline(self) -> AsyncIterator[None]:
        self.round_trips += 1
        self._in_pipeline = True
        try:
            yield
        finally:
            self._in_pipeline = False

    async def rollback(self) -> None:
        self.rolled_back = True
        self.info.transaction_status = IDLE


class FakePool:
    def __init__(self, conns: list[FakeConn]) -> None:
        self._conns = iter(conns)
        self.checks = 0

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[FakeConn]:
        yield next(self._conns)

    async def check(self) -> None:
        self.checks += 1


def _run[T](conn: FakeConn, flow: Flow[T], *, slow: bool = False) -> T:
    return asyncio.run(run_flow(cast(Any, conn), flow, slow=slow))


def _retry[T](pool: FakePool, make: Callable[[], Flow[T]], stats: RetryStats) -> T:
    return asyncio.run(run_with_retry(cast(Pool, pool), make, stats))


@pytest.fixture(autouse=True)
def _pipeline_and_no_sleep(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Use the pipeline path whatever libpq is installed, and never wait between retries."""
    monkeypatch.setattr(postgres_driver, "_PIPELINE", True)

    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("spendgate.ledger.postgres_driver.asyncio.sleep", no_sleep)
    yield


def _read_then_finish() -> Flow[int]:
    rows = yield q("SELECT n FROM t WHERE id = :id", id="a")
    yield from finish(q("UPDATE t SET n = :n WHERE id = :id", n=1, id="a"))
    return len(rows)


def test_begin_rides_with_the_first_statement_and_the_last_writes_with_the_commit() -> None:
    conn = FakeConn(lambda s: [{"n": 1}] if s.startswith("SELECT") else None)
    assert _run(conn, _read_then_finish()) == 1
    assert conn.sent[0].startswith("BEGIN")
    assert conn.sent[1].startswith("SELECT")
    assert conn.sent[2].startswith("UPDATE")
    assert conn.sent[3] == "COMMIT"
    assert conn.round_trips == 2  # BEGIN + SELECT, then UPDATE + COMMIT


def test_a_batch_gets_one_row_list_per_statement_in_one_round_trip() -> None:
    def flow() -> Generator[Any, list[Row], list[list[Row]]]:
        results = yield from batch(q("SELECT 1 AS a"), q("SELECT 2 AS b"))
        return results

    conn = FakeConn(lambda s: [{"v": s}])
    results = _run(conn, flow())
    assert [r[0]["v"] for r in results] == ["SELECT 1 AS a", "SELECT 2 AS b"]
    assert conn.sent[-1] == "COMMIT"
    assert conn.round_trips == 2  # BEGIN + both SELECTs, then the COMMIT


def test_without_pipeline_support_each_statement_is_its_own_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(postgres_driver, "_PIPELINE", False)
    conn = FakeConn()
    _run(conn, _read_then_finish())
    assert conn.round_trips == len(conn.sent) == 4


def test_a_slow_flow_raises_the_statement_timeout_in_its_first_round_trip() -> None:
    conn = FakeConn()
    _run(conn, _read_then_finish(), slow=True)
    assert "statement_timeout" in conn.sent[1]
    assert conn.round_trips == 2


def test_a_flow_that_commits_itself_and_ends_is_not_committed_twice() -> None:
    def flow() -> Flow[str]:
        yield q("UPDATE t SET n = 1")
        yield Tx.COMMIT
        return "done"

    conn = FakeConn()
    assert _run(conn, flow()) == "done"
    assert conn.sent.count("COMMIT") == 1


def test_a_flow_that_ends_in_a_transaction_is_committed() -> None:
    def flow() -> Flow[None]:
        yield q("UPDATE t SET n = 1")

    conn = FakeConn()
    _run(conn, flow())
    assert conn.sent[-1] == "COMMIT"


def test_a_failing_statement_rolls_back_and_raises() -> None:
    conn = FakeConn()
    conn.fail_on = "SELECT"
    with pytest.raises(RuntimeError, match="boom"):
        _run(conn, _read_then_finish())
    assert conn.rolled_back
    assert "COMMIT" not in conn.sent


def test_a_connection_lost_before_the_commit_is_reported_as_dropped() -> None:
    conn = FakeConn()
    conn.fail_on = "SELECT"
    conn.failure = psycopg.OperationalError("server closed the connection")
    conn.broken = True
    with pytest.raises(ConnectionDropped):
        _run(conn, _read_then_finish())


def test_a_connection_lost_with_the_commit_in_flight_is_not_reported_as_dropped() -> None:
    conn = FakeConn()
    conn.fail_on = "COMMIT"
    conn.failure = psycopg.OperationalError("server closed the connection")
    conn.broken = True
    with pytest.raises(psycopg.OperationalError):
        _run(conn, _read_then_finish())


def test_a_dropped_connection_is_retried_on_a_fresh_one_after_a_pool_check() -> None:
    dead = FakeConn()
    dead.fail_on = "SELECT"
    dead.failure = psycopg.OperationalError("gone")
    dead.broken = True
    pool = FakePool([dead, FakeConn()])
    stats = RetryStats()
    assert _retry(pool, _read_then_finish, stats) == 0
    assert pool.checks == 1
    assert stats.by_sqlstate["08006"] == 1


def test_a_connection_that_keeps_dropping_ends_in_ledger_unavailable() -> None:
    conns = []
    for _ in range(MAX_RETRIES + 1):
        c = FakeConn()
        c.fail_on, c.failure, c.broken = "SELECT", psycopg.OperationalError("gone"), True
        conns.append(c)
    stats = RetryStats()
    with pytest.raises(LedgerUnavailable, match="connection lost"):
        _retry(FakePool(conns), _read_then_finish, stats)
    assert stats.exhausted == 1


def test_a_deadlock_is_retried_and_counted_by_sqlstate() -> None:
    first = FakeConn()
    first.fail_on, first.failure = "SELECT", errors.DeadlockDetected("deadlock detected")
    stats = RetryStats()
    _retry(FakePool([first, FakeConn()]), _read_then_finish, stats)
    assert stats.by_sqlstate["40P01"] == 1
    assert stats.total == 1


def test_conflicts_on_every_attempt_end_in_ledger_unavailable() -> None:
    conns = []
    for _ in range(MAX_RETRIES + 1):
        c = FakeConn()
        c.fail_on, c.failure = "SELECT", errors.SerializationFailure("could not serialize")
        conns.append(c)
    stats = RetryStats()
    with pytest.raises(LedgerUnavailable, match="conflicted"):
        _retry(FakePool(conns), _read_then_finish, stats)
    assert stats.exhausted == 1
    assert stats.by_sqlstate["40001"] == MAX_RETRIES + 1


def test_any_other_database_error_is_ledger_unavailable_at_once() -> None:
    conn = FakeConn()
    conn.fail_on, conn.failure = "SELECT", errors.UndefinedTable("no such table")
    stats = RetryStats()
    with pytest.raises(LedgerUnavailable, match="ledger error"):
        _retry(FakePool([conn]), _read_then_finish, stats)
    assert stats.total == 0


def test_a_lost_race_to_create_a_row_is_retried() -> None:
    """A unique violation outside `reservations` (no constraint name here) is a lost race."""
    first = FakeConn()
    first.fail_on, first.failure = "SELECT", errors.UniqueViolation("duplicate key")
    stats = RetryStats()
    _retry(FakePool([first, FakeConn()]), _read_then_finish, stats)
    assert stats.by_sqlstate["23505"] == 1
