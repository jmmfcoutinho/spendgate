"""How many round trips a money transition costs, and the room it reports (issue #7).

A flow step is one round trip on Postgres: a statement, or a `Batch` sent in pipeline mode with
`BEGIN` riding on the first. A flow that does not end in a `last` batch pays one more for its
COMMIT. Counting steps is backend independent, so these run on every adapter; the wire-level
numbers are in docs/reviews/load-v0.1.md.
"""

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from spendgate.core.budgets import Period
from spendgate.ledger import sql
from spendgate.ledger.common import i
from spendgate.ledger.engine import LedgerEngine
from spendgate.ledger.port import BudgetRoom, ConflictError, LedgerUnavailable
from spendgate.ledger.sql import Batch, Flow, Row, Stmt, Tx, q
from tests.contract.rig import LEASE_MS, SECOND, T0, E, Rig, World


def _counting[T](make_flow: Callable[[], Flow[T]], steps: list[Stmt | Batch | Tx]) -> Flow[T]:
    flow = make_flow()
    try:
        step = next(flow)
        while True:
            steps.append(step)
            step = flow.send((yield step))
    except StopIteration as done:
        result: T = done.value
        return result


class Trips:
    """Wraps the ledger's `_run` and records each flow's steps."""

    def __init__(self, ledger: LedgerEngine, monkeypatch: pytest.MonkeyPatch) -> None:
        self.flows: list[list[Stmt | Batch | Tx]] = []
        run = ledger._run

        async def counted(make_flow: Callable[[], Flow[Any]], **kw: bool) -> Any:  # noqa: ANN401
            steps: list[Stmt | Batch | Tx] = []
            self.flows.append(steps)
            return await run(lambda: _counting(make_flow, steps), **kw)

        monkeypatch.setattr(ledger, "_run", counted)

    def round_trips(self) -> list[int]:
        """Per flow: its steps, plus the COMMIT unless the last batch carried it."""
        return [
            len(s) + (0 if s and isinstance(s[-1], Batch) and s[-1].last else 1) for s in self.flows
        ]


@pytest.fixture
def trips(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Iterator[Trips]:
    assert isinstance(rig.ledger, LedgerEngine)
    yield Trips(rig.ledger, monkeypatch)


def three_budgets(rig: Rig) -> World:
    """A key scope with two budgets under an org with a third: the hold locks all three."""
    w = rig.world("10", org_cap="20")
    second = rig.run(rig.ledger.create_budget(w.scope.id, Period.MONTH, E("5"), now=T0))
    return World(w.scope, w.budget, w.key, w.parent, (*w.extra, second))


def test_a_hold_is_three_round_trips_however_many_budgets_it_locks(rig: Rig, trips: Trips) -> None:
    w = three_budgets(rig)
    trips.flows.clear()
    held = rig.hold_ok(w, "r1", "1")
    assert len(held.items) == 3
    assert trips.round_trips() == [3]  # unlocked reads; budget locks; writes with the COMMIT


def test_a_settle_and_a_release_are_two_round_trips_each(rig: Rig, trips: Trips) -> None:
    w = three_budgets(rig)
    rig.hold_ok(w, "r1", "1")
    rig.hold_ok(w, "r2", "1")
    trips.flows.clear()
    assert rig.settle("r1", "0.5").applied
    assert rig.release("r2").applied
    assert trips.round_trips() == [2, 2]  # guarded update and locks; bookings with the COMMIT


def test_renewing_many_leases_is_one_statement(rig: Rig, trips: Trips) -> None:
    w = rig.world("100")
    ids = {f"r{n}" for n in range(12)}
    for rid in sorted(ids):
        rig.hold_ok(w, rid, "1")
    rig.settle("r0", "1")  # no longer pending: not renewed
    trips.flows.clear()
    renewed = rig.run(rig.ledger.renew_all("A", ids | {"unknown"}, T0 + SECOND, LEASE_MS))
    assert renewed == tuple(sorted(ids - {"r0"}))
    assert [len(steps) for steps in trips.flows] == [1]
    assert rig.run(rig.ledger.get_reservation("r5")).lease_expires_at == T0 + SECOND + LEASE_MS  # type: ignore[union-attr]
    assert rig.run(rig.ledger.renew_all("B", ids, T0 + SECOND, LEASE_MS)) == ()  # not B's holds


def test_the_room_a_hold_reports_is_what_a_fresh_read_shows(rig: Rig) -> None:
    w = three_budgets(rig)
    rig.hold_ok(w, "r0", "2")
    rig.settle("r0", "1.5")
    held = rig.hold_ok(w, "r1", "1")
    after = {
        t.budget_id: t for b in (w.scope.id, w.parent.id) for t in rig.run(rig.ledger.totals(b, T0))
    }
    assert {r.budget_id: r.room_nano for r in held.rooms} == {
        b: t.effective_cap_nano - t.spent_nano - t.held_nano for b, t in after.items()
    }
    assert {r.scope_id for r in held.rooms} == {w.scope.id, w.parent.id}


def test_the_room_a_settlement_reports_is_what_a_fresh_read_shows_also_after_an_overrun(
    rig: Rig,
) -> None:
    w = rig.world("10", org_cap="3")
    rig.hold_ok(w, "r1", "2")
    rig.hold_ok(w, "r2", "1")
    done = rig.settle("r1", "2.5")  # 0.5 over the hold: the org budget goes below zero
    assert done.overrun_nano == E("0.5")
    settled = {r.budget_id: r.room_nano for r in done.rooms}
    assert settled == {w.budget.id: E("6.5"), w.extra[0].id: -E("0.5")}  # r2 still holds 1
    released = {r.budget_id: r.room_nano for r in rig.release("r2").rooms}
    assert released == {w.budget.id: E("7.5"), w.extra[0].id: E("0.5")}
    for budget in (w.budget, w.extra[0]):
        t = rig.totals(w, budget=budget)
        assert released[budget.id] == t.effective_cap_nano - t.spent_nano - t.held_nano


def test_a_settlement_that_was_not_applied_reports_no_room(rig: Rig) -> None:
    w = rig.world("10")
    rig.hold_ok(w, "r1", "1")
    assert rig.settle("r1", "1").rooms
    again = rig.settle("r1", "1")
    assert not again.applied and again.rooms == ()


def test_a_disabled_budget_is_booked_but_reports_no_room(rig: Rig) -> None:
    w = rig.world("10", org_cap="20")
    rig.hold_ok(w, "r1", "1")
    rig.disable_budget(w.extra[0].id)
    done = rig.settle("r1", "1")
    assert [r.budget_id for r in done.rooms] == [w.budget.id]
    assert rig.totals(w).spent_nano == E("1")


def last_then_more(steps: list[str]) -> Flow[int]:
    """A broken flow: it yields again after its last batch."""
    yield from sql.finish(q(sql.KEY_INSERT, id="k-late", scope_id=steps[0], now=T0))
    steps.append("resumed")
    rows: list[Row] = yield q("SELECT 1 AS n")
    return i(rows[0], "n")


def test_a_flow_that_goes_on_after_its_last_batch_commits_nothing(rig: Rig) -> None:
    w = rig.world("10")
    steps = [w.scope.id]
    assert isinstance(rig.ledger, LedgerEngine)
    with pytest.raises(RuntimeError, match="after its last batch"):
        rig.run(rig.ledger._run(lambda: last_then_more(steps)))
    assert steps[-1] == "resumed"  # the flow was resumed before anything was sent
    assert rig.run(rig.ledger.get_key("k-late")) is None


def test_a_failing_last_batch_rolls_the_whole_hold_back(rig: Rig) -> None:
    w = rig.world("10")
    start = rig.hold_ok(w, "r1", "1").items[0].period_start
    before = rig.totals(w)
    assert isinstance(rig.ledger, LedgerEngine)
    with pytest.raises((ConflictError, LedgerUnavailable)):  # each adapter names it its own way
        rig.run(rig.ledger._run(lambda: duplicate_insert(w, start)))
    assert rig.totals(w) == before


def duplicate_insert(w: World, start: int) -> Flow[None]:
    """Book a hold on the budget, then insert a reservation id that exists, in the last batch."""
    yield from sql.finish(
        q(sql.PERIOD_HOLD, budget_id=w.budget.id, period_start=start, worst=E("1"), excess=0,
          soft=None),
        q(sql.RES_INSERT, id="r1", owner="A", lease=T0, key_id=w.key.id, scope_id=w.scope.id,
          session_id=None, meta="{}", now=T0),
    )  # fmt: skip


def test_rooms_name_their_owning_scope(rig: Rig) -> None:
    w = rig.world("10", org_cap="20")
    held = rig.hold_ok(w, "r1", "4")
    assert sorted(held.rooms, key=lambda r: r.budget_id) == sorted(
        [
            BudgetRoom(w.budget.id, w.scope.id, E("6")),
            BudgetRoom(w.extra[0].id, w.parent.id, E("16")),
        ],
        key=lambda r: r.budget_id,
    )


JAN_31_LAST_SECOND = 1_769_903_999_000  # 2026-01-31T23:59:59Z
FEB_1 = 1_769_904_001_000  # 2026-02-01T00:00:01Z


def test_a_settlement_across_a_month_boundary_reports_no_room_for_the_old_period(rig: Rig) -> None:
    w = rig.world("10", period=Period.MONTH, now=JAN_31_LAST_SECOND)
    rig.hold_ok(w, "spent", "6", JAN_31_LAST_SECOND)
    rig.settle("spent", "6", JAN_31_LAST_SECOND)
    held = rig.hold_ok(w, "late", "3", JAN_31_LAST_SECOND)
    assert [r.room_nano for r in held.rooms] == [E("1")]  # January: 10 - 6 - 3
    done = rig.settle("late", "1", FEB_1)  # booked into January, where the hold was taken
    assert done.applied and done.rooms == ()  # January's room is not what a header shows now
    february = rig.totals(w, FEB_1)
    assert february.effective_cap_nano - february.spent_nano - february.held_nano == E("10")
