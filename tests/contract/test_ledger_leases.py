"""Ledger contract: leases, the sweeper, owner-versus-sweeper races and late settles."""

import asyncio

from spendgate.core.money import Money
from spendgate.core.outcomes import Outcome, ReservationState, charge
from tests.contract.rig import LEASE_MS, SECOND, T0, E, Rig, attempt
from tests.contract.test_ledger import assert_i1, figures, spend

# --- scenarios 2, 3, 5: leases ---------------------------------------------------------------


def test_scenario_02_crash_mid_call_is_charged_in_full_by_the_sweeper(rig: Rig) -> None:
    w = rig.world("20")
    spend(rig, w, "9.40")
    rig.hold_ok(w, "A", "1")  # the owner dies here: no renewals, no settle
    assert rig.sweep(T0 + 59_900) == ()
    assert figures(rig.totals(w)) == (E("9.40"), E("1"), 0, 0)
    assert rig.sweep(T0 + 60_100) == ("A",)
    assert rig.sweep(T0 + 60_200) == ()  # a second sweep finds nothing to charge
    assert figures(rig.totals(w)) == (E("10.40"), 0, 0, 0)
    (row,) = rig.run(rig.ledger.get_calls("A"))
    assert (row.outcome, row.settled_by, row.charged_nano) == (Outcome.EXPIRED, "sweeper", E("1"))
    assert row.usage_cost_nano is None
    reservation = rig.run(rig.ledger.get_reservation("A"))
    assert reservation is not None and reservation.state is ReservationState.EXPIRED
    assert reservation.charged_nano == E("1")


def test_scenario_03_stream_lease_renewal_then_cut_is_charged_the_full_hold(rig: Rig) -> None:
    w = rig.world("20")
    spend(rig, w, "10.40")
    rig.hold_ok(w, "S", "1")
    assert rig.run(rig.ledger.renew_all("A", {"S"}, T0 + 20 * SECOND, LEASE_MS)) == ("S",)
    assert rig.run(rig.ledger.renew_all("A", {"S"}, T0 + 40 * SECOND, LEASE_MS)) == ("S",)
    assert rig.sweep(T0 + 70 * SECOND) == ()  # the original lease ended at +60 s, renewal kept it
    hold = Money(E("1"), "EUR")
    cut = charge(Outcome.STREAM_BROKEN, None, hold)  # no trustworthy usage after the cut
    rig.settle("S", "1", T0 + 75 * SECOND, outcome=Outcome.STREAM_BROKEN)
    assert cut == hold
    assert figures(rig.totals(w)) == (E("11.40"), 0, 0, 0)


def test_renew_all_extends_only_that_instances_pending_holds(rig: Rig) -> None:
    w = rig.world("20")
    rig.hold_ok(w, "mine", "1", instance="A")
    rig.hold_ok(w, "theirs", "1", instance="B")
    rig.hold_ok(w, "done", "1", instance="A")
    rig.settle("done", "0.1")
    named = {"mine", "done", "theirs"}  # "done" is settled, "theirs" is B's: neither is renewed
    assert rig.run(rig.ledger.renew_all("A", named, T0 + 30 * SECOND, LEASE_MS)) == ("mine",)
    mine, theirs = (rig.run(rig.ledger.get_reservation(r)) for r in ("mine", "theirs"))
    assert mine is not None and theirs is not None
    assert mine.lease_expires_at == T0 + 30 * SECOND + LEASE_MS
    assert theirs.lease_expires_at == T0 + LEASE_MS


def test_cr_02_renew_all_leaves_a_hold_the_caller_did_not_name_to_the_sweeper(rig: Rig) -> None:
    """The owner failed to close "stuck": it is no longer named, so its lease runs out."""
    w = rig.world("20")
    rig.hold_ok(w, "live", "1", instance="A")
    rig.hold_ok(w, "stuck", "1", instance="A")
    for beat in range(1, 11):  # ten heartbeats of 20 s name only the call still in flight
        got = rig.run(rig.ledger.renew_all("A", {"live"}, T0 + beat * 20 * SECOND, LEASE_MS))
        assert got == ("live",)
    assert rig.run(rig.ledger.renew_all("A", set(), T0 + 200 * SECOND, LEASE_MS)) == ()
    assert rig.sweep(T0 + 200 * SECOND) == ("stuck",)  # charged in full; "live" is untouched
    assert figures(rig.totals(w, T0 + 200 * SECOND)) == (E("1"), E("1"), 0, 0)


def test_scenario_05_other_instances_sweeper_leaves_a_live_hold_alone(rig: Rig) -> None:
    w = rig.world("20")
    rig.hold_ok(w, "A1", "1", instance="A")
    rig.run(rig.ledger.renew_all("B", {"A1"}, T0 + 10 * SECOND, LEASE_MS))  # B cannot renew A's
    assert rig.sweep(T0 + 10 * SECOND) == ()  # B's sweeper: A's lease has not ended
    rig.settle("A1", "0.50", T0 + 11 * SECOND)
    assert figures(rig.totals(w)) == (E("0.50"), 0, 0, 0)


def test_scenario_05_restarted_instance_never_touches_its_old_hold(rig: Rig) -> None:
    w = rig.world("20")
    rig.hold_ok(w, "old", "1", instance="A")
    renewed = rig.run(rig.ledger.renew_all("A2", {"old"}, T0 + 10 * SECOND, LEASE_MS))
    assert renewed == ()  # new identity
    assert rig.sweep(T0 + LEASE_MS - 1) == ()
    assert rig.sweep(T0 + LEASE_MS + 1) == ("old",)  # any sweeper charges it when the lease ends
    assert figures(rig.totals(w)) == (E("1"), 0, 0, 0)


def test_restarting_one_process_never_touches_any_hold(rig: Rig) -> None:
    w = rig.world("20")
    rig.hold_ok(w, "A", "2", instance="A")
    rig.hold_ok(w, "B", "3", instance="B")
    before = [rig.run(rig.ledger.get_reservation(r)) for r in ("A", "B")]
    totals_before = rig.totals(w)
    rig.restart()  # a process restart: close and open the same store again
    after = [rig.run(rig.ledger.get_reservation(r)) for r in ("A", "B")]
    assert after == before
    assert rig.totals(w) == totals_before
    assert all(r is not None and r.state is ReservationState.PENDING for r in after)


# --- scenario 12: owner versus sweeper -------------------------------------------------------


def test_scenario_12_t_mon_10_owner_first_then_sweeper_is_a_no_op(rig: Rig) -> None:
    w = rig.world("20")
    rig.hold_ok(w, "A", "1")
    assert rig.settle("A", "0.40", T0 + 59 * SECOND).applied
    assert rig.sweep(T0 + 61 * SECOND) == ()
    assert figures(rig.totals(w)) == (E("0.40"), 0, 0, 0)


def test_scenario_12_t_mon_10_sweeper_first_then_owner_is_one_money_transition(rig: Rig) -> None:
    w = rig.world("20")
    rig.hold_ok(w, "A", "1")
    assert rig.sweep(T0 + 60_100) == ("A",)
    late = rig.settle("A", "0.40", T0 + 60_200, usage="0.40")  # 0.40 <= 1.00: no money change
    assert (late.late, late.charged_nano, late.state) == (True, 0, ReservationState.EXPIRED)
    assert figures(rig.totals(w)) == (E("1"), 0, 0, 0)
    (row,) = rig.run(rig.ledger.get_calls("A"))  # the sweeper's row, updated, still one row
    assert (row.outcome, row.settled_by) == (Outcome.OK, "sweeper")
    assert (row.charged_nano, row.usage_cost_nano) == (E("1"), E("0.40"))


def test_t_mon_10_owner_and_sweeper_race_has_one_money_transition(rig: Rig) -> None:
    """Real concurrency: whoever wins, money moves once. Which one wins is up to the backend's
    scheduler (SQLite: a thread lock), so this test cannot force an order; the two ordered tests
    above force each interleaving deterministically (CR-10), and this one asserts the invariant
    over whatever mix happened."""
    w = rig.world("1000")
    for n in range(15):
        rid, now = f"race-{n}", T0 + 61 * SECOND
        rig.hold_ok(w, rid, "1")

        async def both(rid: str = rid, now: int = now) -> None:
            await asyncio.gather(
                rig.ledger.settle(rid, E("0.40"), (attempt(1, Outcome.OK, E("0.40"), now),), now),
                rig.ledger.sweep_expired(now),
            )

        rig.run(both())
        row = rig.run(rig.ledger.get_reservation(rid))
        assert row is not None and row.state in {ReservationState.SETTLED, ReservationState.EXPIRED}
        assert len(rig.run(rig.ledger.get_calls(rid))) == 1  # one calls row either way
    t = rig.totals(w, T0)
    settled = sum(1 for n in range(15) if _state(rig, f"race-{n}") is ReservationState.SETTLED)
    assert t.held_nano == 0
    assert t.spent_nano == settled * E("0.40") + (15 - settled) * E("1")  # never both
    assert_i1(t)


def _state(rig: Rig, rid: str) -> ReservationState:
    row = rig.run(rig.ledger.get_reservation(rid))
    assert row is not None
    return row.state


# --- scenario 13: late settle ----------------------------------------------------------------


# T-MON-03
def test_scenario_13_m04_late_settle_books_the_overrun_and_updates_the_sweepers_row(
    rig: Rig,
) -> None:
    """The reservation row shows what was booked: the hold plus the late overrun."""
    w = rig.world("10")
    spend(rig, w, "8")
    rig.hold_ok(w, "A", "1")
    assert rig.sweep(T0 + 60_100) == ("A",)  # the owner was partitioned: sweeper charges 1.00
    assert figures(rig.totals(w)) == (E("9"), 0, 0, 0)
    late = rig.settle("A", "1.80", T0 + 61 * SECOND, usage="1.80")
    assert (late.late, late.overrun_nano, late.state) == (True, E("0.80"), ReservationState.EXPIRED)
    t = rig.totals(w)
    assert figures(t) == (E("9.80"), 0, E("0.80"), 0) and t.review_required_at is None
    assert_i1(t)
    reservation = rig.run(rig.ledger.get_reservation("A"))
    assert reservation is not None and reservation.state is ReservationState.EXPIRED
    assert reservation.charged_nano == E("1.80")  # the swept hold 1.00 plus the overrun 0.80
    assert reservation.item_overruns == {w.budget.id: E("0.80")}
    (row,) = rig.run(rig.ledger.get_calls("A"))
    assert (row.outcome, row.settled_by) == (Outcome.OK, "sweeper")
    assert (row.usage_cost_nano, row.charged_nano, row.overrun_nano) == (
        E("1.80"),
        E("1.80"),
        E("0.80"),
    )


def test_late_settle_twice_changes_nothing(rig: Rig) -> None:
    w = rig.world("10")
    rig.hold_ok(w, "A", "1")
    rig.sweep(T0 + 60_100)
    assert rig.settle("A", "1.80", T0 + 61 * SECOND, usage="1.80").late
    before = rig.totals(w)
    again = rig.settle("A", "1.80", T0 + 62 * SECOND, usage="1.80")
    assert (again.applied, again.charged_nano) == (False, 0)
    assert rig.totals(w) == before
    assert len(rig.run(rig.ledger.get_calls("A"))) == 1
    reservation = rig.run(rig.ledger.get_reservation("A"))
    assert reservation is not None and reservation.charged_nano == E("1.80")


def test_m04_late_settle_under_the_hold_keeps_the_reservation_at_the_swept_hold(rig: Rig) -> None:
    """`charge <= held` books nothing more, so the reservation still shows the full hold."""
    w = rig.world("10")
    rig.hold_ok(w, "A", "1")
    rig.sweep(T0 + 60_100)
    late = rig.settle("A", "0.40", T0 + 61 * SECOND, usage="0.40")
    assert (late.late, late.overrun_nano) == (True, 0)
    reservation = rig.run(rig.ledger.get_reservation("A"))
    assert reservation is not None and reservation.state is ReservationState.EXPIRED
    assert reservation.charged_nano == E("1")
    assert figures(rig.totals(w)) == (E("1"), 0, 0, 0)


def test_late_settle_with_attempts_rewrites_the_sweeper_row_as_the_consuming_attempt(
    rig: Rig,
) -> None:
    w = rig.world("10")
    rig.hold_ok(w, "A", "1", providers=("p1", "p2"))
    rig.sweep(T0 + 60_100)
    attempts = (
        attempt(1, Outcome.THROTTLED, 0, T0 + 1, True, provider="p1"),
        attempt(2, Outcome.OK, E("1.50"), T0 + 2, usage_cost_nano=E("1.50"), provider="p2"),
    )
    rig.settle("A", "1.50", T0 + 61 * SECOND, attempts=attempts)
    rows = rig.run(rig.ledger.get_calls("A"))
    assert [(r.attempt, r.outcome, r.charged_nano, r.settled_by) for r in rows] == [
        (1, Outcome.THROTTLED, 0, "owner"),
        (2, Outcome.OK, E("1.50"), "sweeper"),
    ]
    assert figures(rig.totals(w)) == (E("1.50"), 0, E("0.50"), 0)
