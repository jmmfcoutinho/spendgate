"""Postgres: fewer round trips without weaker guarantees (issue #7).

No connection check on checkout, so a connection the server dropped is found by the first
statement; a flow is re-run only when its COMMIT was not yet sent. Reports never lock budget rows.
Batches go out in pipeline mode, or one by one where libpq cannot pipeline.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest

from spendgate.ledger import postgres_driver, sql
from spendgate.ledger.common import i
from spendgate.ledger.port import Held, LedgerUnavailable
from spendgate.ledger.postgres import PostgresLedger
from spendgate.ledger.sql import Flow, q
from tests.contract.rig import CURRENCY, T0, Rig, workdir_location

pytestmark = pytest.mark.postgres

SELF_DESTRUCT = "SELECT pg_terminate_backend(pg_backend_pid())"


@pytest.fixture
def pg(postgres_url: str, tmp_path: Path, new_id: Callable[[], str]) -> Iterator[Rig]:
    made = Rig("postgres", workdir_location("postgres", tmp_path), new_id)
    yield made
    made.close()


def pg_ledger(rig: Rig) -> PostgresLedger:
    assert isinstance(rig.ledger, PostgresLedger)
    return rig.ledger


def dies_once(attempts: list[int], *, at_commit: bool) -> Flow[int]:
    """Kill this flow's own server session on the first run: mid-transaction or with the COMMIT."""
    attempts.append(1)
    if len(attempts) == 1 and not at_commit:
        yield q(SELF_DESTRUCT)
    rows = yield q("SELECT 7 AS n")
    if len(attempts) == 1 and at_commit:
        yield from sql.finish(q(SELF_DESTRUCT))
    return i(rows[0], "n")


def test_a_connection_lost_before_the_commit_is_re_run_on_a_fresh_one(pg: Rig) -> None:
    attempts: list[int] = []
    ledger = pg_ledger(pg)
    assert pg.run(ledger._run(lambda: dies_once(attempts, at_commit=False))) == 7
    assert len(attempts) == 2
    assert ledger.stats.by_sqlstate["08006"] == 1


def test_a_connection_lost_with_the_commit_in_flight_is_not_re_run(pg: Rig) -> None:
    attempts: list[int] = []
    ledger = pg_ledger(pg)
    with pytest.raises(LedgerUnavailable):  # it may have committed: never run a flow twice
        pg.run(ledger._run(lambda: dies_once(attempts, at_commit=True)))
    assert len(attempts) == 1
    assert pg.run(ledger._run(lambda: dies_once([1], at_commit=False))) == 7  # the pool recovers


def test_a_pooled_connection_the_server_dropped_does_not_fail_the_next_hold(pg: Rig) -> None:
    w = pg.world("100")
    ledger = pg.run(PostgresLedger.open(pg.location, currency=CURRENCY, max_size=1))
    try:
        assert isinstance(pg.run(ledger.hold(pg.request(w, "r1", "1"))), Held)
        pid = pg.run(ledger._run(backend_pid))
        with psycopg.connect(pg.location, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        assert isinstance(pg.run(ledger.hold(pg.request(w, "r2", "1"))), Held)
        assert ledger.stats.by_sqlstate["08006"] == 1  # found dead by the hold, which re-ran
        assert pg.run(ledger._run(backend_pid)) != pid
    finally:
        pg.run(ledger.aclose())


def test_dropped_connections_are_given_up_on_after_the_retries(pg: Rig) -> None:
    ledger = pg_ledger(pg)
    runs: list[int] = []

    def always_dies() -> Flow[int]:
        runs.append(1)
        yield q(SELF_DESTRUCT)
        return 0

    with pytest.raises(LedgerUnavailable, match="connection lost"):
        pg.run(ledger._run(always_dies))
    assert len(runs) == postgres_driver.MAX_RETRIES + 1
    assert ledger.stats.exhausted == 1


def backend_pid() -> Flow[int]:
    rows = yield q("SELECT pg_backend_pid() AS pid")
    return i(rows[0], "pid")


def test_a_budget_report_does_not_queue_behind_a_locked_budget_row(pg: Rig) -> None:
    w = pg.world("100")
    pg.hold_ok(w, "r1", "1")
    with psycopg.connect(pg.location, autocommit=True) as other:
        other.execute("BEGIN")
        other.execute("SELECT id FROM budgets WHERE id = %s FOR UPDATE", (w.budget.id,))
        rows = pg.run(pg.ledger.totals(w.scope.id, T0))  # a lock wait would end in lock_timeout
        other.execute("ROLLBACK")
    assert [(r.budget_id, r.held_nano > 0) for r in rows] == [(w.budget.id, True)]


def test_without_pipeline_support_batches_run_one_by_one(
    pg: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(postgres_driver, "_PIPELINE", False)
    w = pg.world("10", org_cap="20")
    held = pg.hold_ok(w, "r1", "4")
    assert len(held.items) == 2
    assert pg.settle("r1", "3").applied
    assert pg.totals(w).spent_nano == pg.totals(w, budget=w.extra[0]).spent_nano > 0
