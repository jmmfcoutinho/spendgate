"""Ledger contract: the money scenarios of ledger-state-machine.md, on every adapter.

Test names carry the money rule (`m13`), the scenario number or the threat-model id so a failure
points at the rule it breaks. Amounts are whole units as strings ("9.40").
"""

import asyncio
from datetime import UTC, datetime

import pytest

from spendgate.core.budgets import Period, PeriodState, invariant_holds
from spendgate.core.errors import ErrorCode
from spendgate.core.money import Money
from spendgate.core.outcomes import Outcome, ReservationState, charge
from spendgate.ledger.port import BudgetTotals, ConflictError, Held, NotFoundError, Refused
from tests.contract.rig import LEASE_MS, T0, E, Rig, World, attempt


def assert_i1(t: BudgetTotals) -> None:
    """I1: spent + held <= effective cap + overrun + excess."""
    state = PeriodState(t.spent_nano, t.held_nano, t.overrun_nano, t.excess_nano)
    assert invariant_holds(state, t.effective_cap_nano), t


def figures(t: BudgetTotals) -> tuple[int, int, int, int]:
    """(spent, held, overrun, excess)."""
    return t.spent_nano, t.held_nano, t.overrun_nano, t.excess_nano


def spend(rig: Rig, w: World, amount: str, rid: str = "seed") -> None:
    """Put `amount` into spent: a hold settled at exactly its size."""
    rig.hold_ok(w, rid, amount)
    rig.settle(rid, amount)


def refusal_of(result: object) -> ErrorCode:
    assert isinstance(result, Refused), result
    return result.refusal.code


# --- scenario 1 ------------------------------------------------------------------------------


def test_scenario_01_last_euro_race_admits_exactly_one(rig: Rig) -> None:
    w = rig.world("10")
    spend(rig, w, "9")

    async def race() -> list[Held | Refused]:
        return list(
            await asyncio.gather(
                rig.ledger.hold(rig.request(w, "A", "1")),
                rig.ledger.hold(rig.request(w, "B", "1")),
            )
        )

    results = rig.run(race())
    held = [r for r in results if isinstance(r, Held)]
    assert len(held) == 1
    assert [refusal_of(r) for r in results if isinstance(r, Refused)] == [
        ErrorCode.BUDGET_EXHAUSTED
    ]
    assert figures(rig.totals(w)) == (E("9"), E("1"), 0, 0)
    rig.settle(held[0].request_id, "0.40")
    assert figures(rig.totals(w)) == (E("9.40"), 0, 0, 0)
    assert refusal_of(rig.hold(w, "C", "1")) is ErrorCode.BUDGET_EXHAUSTED  # 9.4 + 1 > 10


def test_refused_hold_leaves_no_trace_on_any_budget(rig: Rig) -> None:
    w = rig.world("10", org_cap="1")  # the org budget is the one that refuses
    assert refusal_of(rig.hold(w, "A", "2")) is ErrorCode.BUDGET_EXHAUSTED
    assert figures(rig.totals(w)) == (0, 0, 0, 0)  # the user budget held nothing
    assert rig.run(rig.ledger.get_reservation("A")) is None


# --- scenario 4: failover inside one reservation ---------------------------------------------


def test_scenario_04_m01_provider_429_fails_over_inside_one_reservation(rig: Rig) -> None:
    from spendgate.core.sessions import SessionDefaults  # noqa: PLC0415

    w = rig.world("20", session_defaults=SessionDefaults(max_steps=5))
    spend(rig, w, "11.40")
    held = rig.hold_ok(w, "R", "1", session="s1", providers=("p1", "p2"))  # worst = max of both
    assert held.items[0].held_nano == E("1")
    attempts = (
        attempt(1, Outcome.THROTTLED, 0, T0 + 1, True, provider="p1"),
        attempt(2, Outcome.OK, E("0.30"), T0 + 2, usage_cost_nano=E("0.30"), provider="p2"),
    )
    rig.settle("R", "0.30", T0 + 2, attempts=attempts)
    assert figures(rig.totals(w)) == (E("11.70"), 0, 0, 0)
    rows = rig.run(rig.ledger.get_calls("R"))
    assert [(r.attempt, r.outcome, r.charged_nano) for r in rows] == [
        (1, Outcome.THROTTLED, 0),
        (2, Outcome.OK, E("0.30")),
    ]
    assert held.session_scope_id is not None
    session = rig.run(rig.ledger.get_scope(held.session_scope_id))
    assert session is not None and session.step_count == 1  # per request, not per attempt


# --- scenario 6: rollover --------------------------------------------------------------------


def _ms(day: int, hour: int, minute: int, second: int) -> int:
    return int(datetime(2026, 3, day, hour, minute, second, tzinfo=UTC).timestamp() * 1000)


def test_scenario_06_rollover_with_a_hold_in_flight_settles_into_the_old_period(rig: Rig) -> None:
    w = rig.world("100", period=Period.DAY)
    day5, day6 = _ms(5, 23, 59, 58), _ms(6, 0, 0, 1)
    rig.hold_ok(w, "A", "1", day5)
    rig.hold_ok(w, "B", "1", day6)  # a new period row, created at zero
    assert rig.settle("A", "0.30", _ms(6, 0, 0, 3)).applied
    old, new = rig.totals(w, day5), rig.totals(w, day6)
    assert old.period_start != new.period_start
    assert figures(old) == (E("0.30"), 0, 0, 0)
    assert figures(new) == (0, E("1"), 0, 0)
    assert_i1(old)
    assert_i1(new)


def test_t_clk_01_a_clock_jumping_back_and_forward_across_midnight_loses_no_money(rig: Rig) -> None:
    """Each hold stays in the period row of the clock reading it was made at, whatever the clock
    reads when it settles. A backward jump does not end the newer period early or forget its
    spend: when the clock comes back, that spend still counts against its cap."""
    w = rig.world("1", period=Period.DAY)
    day5, day6 = _ms(5, 23, 59, 59), _ms(6, 0, 0, 1)
    rig.hold_ok(w, "fwd", "0.60", day6)  # the clock jumped forward over midnight
    rig.hold_ok(w, "back", "0.60", day5)  # then back again: day 5's row, its own cap
    assert rig.settle("fwd", "0.50", day5).applied  # settled while the clock reads day 5
    assert rig.settle("back", "0.50", day6).applied  # settled while the clock reads day 6
    old, new = rig.totals(w, day5), rig.totals(w, day6)
    assert old.period_start != new.period_start
    assert figures(old) == (E("0.50"), 0, 0, 0)
    assert figures(new) == (E("0.50"), 0, 0, 0)
    assert refusal_of(rig.hold(w, "again", "0.60", day6)) is ErrorCode.BUDGET_PERIOD_EXHAUSTED
    assert refusal_of(rig.hold(w, "again", "0.60", day5)) is ErrorCode.BUDGET_PERIOD_EXHAUSTED
    assert_i1(old)
    assert_i1(new)


# --- scenarios 7 and 16: cap lowered, then raised --------------------------------------------


def test_scenario_07_cap_lowered_below_spent_records_excess_and_blocks(rig: Rig) -> None:
    w = rig.world("10")
    spend(rig, w, "8")
    rig.hold_ok(w, "A", "1")
    rig.run(rig.ledger.update_budget(w.budget.id, actor="admin", now=T0, cap_nano=E("5")))
    assert figures(rig.totals(w)) == (E("8"), E("1"), 0, E("4"))  # excess = 8 + 1 - 5
    assert refusal_of(rig.hold(w, "B", "0.01")) is ErrorCode.BUDGET_EXHAUSTED
    rig.settle("A", "0.60")  # in-flight holds settle normally
    t = rig.totals(w)
    assert figures(t) == (E("8.60"), 0, 0, E("4"))
    assert_i1(t)


# T-MON-06
def test_scenario_16_m08_cap_lowered_then_raised_recomputes_excess(rig: Rig) -> None:
    w = rig.world("10")
    spend(rig, w, "8")
    rig.hold_ok(w, "A", "1")
    rig.run(rig.ledger.update_budget(w.budget.id, actor="admin", now=T0, cap_nano=E("5")))
    rig.settle("A", "0.60")
    rig.run(rig.ledger.update_budget(w.budget.id, actor="admin", now=T0, cap_nano=E("12")))
    t = rig.totals(w)
    assert figures(t) == (E("8.60"), 0, 0, 0)  # excess shrank back to zero
    assert_i1(t)
    assert isinstance(rig.hold(w, "B", "1"), Held)  # 8.6 + 1 <= 12


# --- scenarios 8 and 15: overrun -------------------------------------------------------------


def test_scenario_08_m03_m07_breach_overrun_is_booked_in_full_and_blocks_until_ack(
    rig: Rig,
) -> None:
    w = rig.world("10")
    spend(rig, w, "9")
    rig.hold_ok(w, "A", "1")
    done = rig.settle("A", "1.80")  # the provider billed more than the hold
    assert done.overrun_nano == E("0.80") and done.review_flagged == (w.budget.id,)
    t = rig.totals(w)
    assert figures(t) == (E("10.80"), 0, E("0.80"), 0)  # M-03: the real charge, never clamped
    assert t.review_required_at is not None
    assert_i1(t)
    blocked = rig.hold(w, "B", "0.01")
    assert isinstance(blocked, Refused) and blocked.refusal.details["reason"] == "overrun_review"
    rig.run(rig.ledger.update_budget(w.budget.id, actor="admin", now=T0, cap_nano=E("20")))
    assert (
        refusal_of(rig.hold(w, "B2", "0.01")) is ErrorCode.BUDGET_EXHAUSTED
    )  # a raise is not an ack
    rig.run(rig.ledger.ack_overrun(w.budget.id, actor="admin", reason="checked", now=T0))
    assert isinstance(rig.hold(w, "B3", "0.01"), Held)


# T-MON-05
def test_scenario_15_m07_overrun_inside_the_cap_does_not_block(rig: Rig) -> None:
    w = rig.world("10")
    spend(rig, w, "5")
    rig.hold_ok(w, "A", "1")
    done = rig.settle("A", "1.50")
    assert done.overrun_nano == E("0.50") and done.review_flagged == ()
    t = rig.totals(w)
    assert figures(t) == (E("6.50"), 0, E("0.50"), 0) and t.review_required_at is None
    assert isinstance(rig.hold(w, "B", "1"), Held)


def test_non_strict_budget_never_flags_a_breach(rig: Rig) -> None:
    w = rig.world("10", strict_overrun=False)
    spend(rig, w, "9")
    rig.hold_ok(w, "A", "1")
    assert rig.settle("A", "3").review_flagged == ()
    assert refusal_of(rig.hold(w, "B", "0.01")) is ErrorCode.BUDGET_EXHAUSTED  # plain cap rule


# --- scenario 10: grants ---------------------------------------------------------------------


def test_scenario_10_m06_grant_add_and_expiry_records_excess_lazily(rig: Rig) -> None:
    w = rig.world("10")
    spend(rig, w, "9")
    spend(rig, w, "0.50", "seed2")  # S 9.50
    rig.run(
        rig.ledger.create_grant(
            w.budget.id, amount_nano=E("5"), valid_from=T0, valid_until=T0 + 1000,
            actor="admin", reason="launch week", now=T0,
        )
    )  # fmt: skip
    assert rig.totals(w).effective_cap_nano == E("15") and rig.totals(w).excess_nano == 0
    spend(rig, w, "4", "A")  # 9.5 + 4 <= 15
    rig.hold_ok(w, "C", "1", T0 + 500)  # 13.5 + 1 <= 15
    later = T0 + 1500  # the grant has expired: eff is 10 again
    assert refusal_of(rig.hold(w, "D", "0.01", later)) is ErrorCode.BUDGET_EXHAUSTED
    t = rig.totals(w, later)
    assert (t.effective_cap_nano, t.excess_nano) == (E("10"), E("4.50"))  # recorded despite refusal
    rig.settle("C", "0.60", later + 100)
    t = rig.totals(w, later)
    assert figures(t) == (E("14.10"), 0, 0, E("4.50"))
    assert_i1(t)
    assert refusal_of(rig.hold(w, "E", "0.01", later)) is ErrorCode.BUDGET_EXHAUSTED


def test_sweeper_records_excess_when_a_grant_expires_without_traffic(rig: Rig) -> None:
    w = rig.world("10")
    rig.run(
        rig.ledger.create_grant(
            w.budget.id, amount_nano=E("5"), valid_from=T0, valid_until=T0 + 1000,
            actor="admin", reason="r", now=T0,
        )
    )  # fmt: skip
    spend(rig, w, "14")
    result = rig.run(rig.ledger.sweep_expired(T0 + 2000))
    assert result.normalised == (w.budget.id,)
    t = rig.totals(w, T0 + 2000)
    assert t.excess_nano == E("4")
    assert_i1(t)


# --- scenario 11: release then retry ---------------------------------------------------------


# T-MON-01
def test_scenario_11_m02_release_then_retry(rig: Rig) -> None:
    from spendgate.core.sessions import SessionDefaults  # noqa: PLC0415

    w = rig.world("20", session_defaults=SessionDefaults(max_steps=5))
    held = rig.hold_ok(w, "A", "1", session="s1")
    assert held.session_scope_id is not None
    done = rig.release("A")
    assert (done.applied, done.state) == (True, ReservationState.RELEASED)
    assert figures(rig.totals(w)) == (0, 0, 0, 0)
    session = rig.run(rig.ledger.get_scope(held.session_scope_id))
    assert session is not None and session.step_count == 0  # the step was refunded
    (row,) = rig.run(rig.ledger.get_calls("A"))
    assert (row.outcome, row.charged_nano) == (Outcome.THROTTLED, 0)
    with pytest.raises(ConflictError):
        rig.hold(w, "A", "1", session="s1")  # never the old reservation returned as admitted
    rig.hold_ok(w, "B", "1", session="s1")  # the client retry is a new request
    rig.settle("B", "0.40")
    assert figures(rig.totals(w)) == (E("0.40"), 0, 0, 0)
    session = rig.run(rig.ledger.get_scope(held.session_scope_id))
    assert session is not None and session.step_count == 1


@pytest.mark.parametrize("final", ["pending", "settled", "released", "expired"])
def test_m02_a_hold_on_an_existing_request_id_is_a_conflict_in_every_state(
    rig: Rig, final: str
) -> None:
    w = rig.world("20")
    rig.hold_ok(w, "A", "1")
    if final == "settled":
        rig.settle("A", "0.5")
    elif final == "released":
        rig.release("A")
    elif final == "expired":
        rig.sweep(T0 + LEASE_MS + 1)
    before = rig.totals(w)
    with pytest.raises(ConflictError):
        rig.hold(w, "A", "1")
    assert rig.totals(w) == before  # the conflict changed nothing


def test_m02_release_needs_every_attempt_not_processed(rig: Rig) -> None:
    w = rig.world("20")
    rig.hold_ok(w, "A", "1")
    sent = (attempt(1, Outcome.UPSTREAM_5XX, 0, T0, False),)
    with pytest.raises(ValueError, match="not_processed"):
        rig.release("A", attempts=sent)
    charged = (attempt(1, Outcome.THROTTLED, E("1"), T0, True),)
    with pytest.raises(ValueError, match="not_processed"):
        rig.release("A", attempts=charged)
    assert figures(rig.totals(w)) == (0, E("1"), 0, 0)  # still held, nothing moved


# --- settle idempotency ----------------------------------------------------------------------


def test_settling_twice_changes_nothing(rig: Rig) -> None:
    w = rig.world("20")
    rig.hold_ok(w, "A", "2")
    assert rig.settle("A", "0.50").applied
    before = rig.totals(w)
    again = rig.settle("A", "9")  # a different charge must not matter either
    assert (again.applied, again.state) == (False, ReservationState.SETTLED)
    assert rig.totals(w) == before and len(rig.run(rig.ledger.get_calls("A"))) == 1
    assert not rig.release("A").applied  # nor can a settled reservation be released


def test_release_twice_and_settle_after_release_change_nothing(rig: Rig) -> None:
    w = rig.world("20")
    rig.hold_ok(w, "A", "2")
    assert rig.release("A").applied
    assert not rig.release("A").applied
    assert not rig.settle("A", "1").applied
    assert figures(rig.totals(w)) == (0, 0, 0, 0)


def test_settle_of_an_unknown_request_is_not_found(rig: Rig) -> None:
    rig.world("20")
    with pytest.raises(NotFoundError):
        rig.settle("ghost", "1")


# --- scenarios 17-19 and M-13 ----------------------------------------------------------------


def test_m13_cancel_after_send_is_charged_the_full_hold(rig: Rig) -> None:
    w = rig.world("20")
    rig.hold_ok(w, "A", "1")
    hold = Money(E("1"), "EUR")
    booked = charge(Outcome.CANCELLED, None, hold)  # sent, no usage chunk seen
    assert booked == hold
    rig.settle("A", "1", outcome=Outcome.CANCELLED)
    assert figures(rig.totals(w)) == (E("1"), 0, 0, 0)
    (row,) = rig.run(rig.ledger.get_calls("A"))
    assert (row.outcome, row.charged_nano, row.usage_cost_nano) == (Outcome.CANCELLED, E("1"), None)


def test_m13_cancel_after_usage_was_seen_is_charged_that_usage(rig: Rig) -> None:
    w = rig.world("20")
    rig.hold_ok(w, "A", "1")
    seen = Money(E("0.30"), "EUR")
    booked = charge(Outcome.CANCELLED, seen, Money(E("1"), "EUR"))
    assert booked == seen
    rig.settle("A", "0.30", outcome=Outcome.CANCELLED, usage="0.30")
    assert figures(rig.totals(w)) == (E("0.30"), 0, 0, 0)
    (row,) = rig.run(rig.ledger.get_calls("A"))
    assert row.usage_cost_nano == E("0.30")


def test_m13_never_sent_is_released_at_zero(rig: Rig) -> None:
    w = rig.world("20")
    rig.hold_ok(w, "A", "1")
    never_sent = (attempt(1, Outcome.UNREACHABLE, 0, T0, True),)
    assert rig.release("A", attempts=never_sent).state is ReservationState.RELEASED
    assert figures(rig.totals(w)) == (0, 0, 0, 0)


def test_scenario_18_connect_failure_before_send_fails_over_at_cost_zero(rig: Rig) -> None:
    w = rig.world("20")
    spend(rig, w, "1")
    rig.hold_ok(w, "A", "1", providers=("p1", "p2"))
    attempts = (
        attempt(1, Outcome.UNREACHABLE, 0, T0 + 1, True, provider="p1"),
        attempt(2, Outcome.OK, E("0.30"), T0 + 2, usage_cost_nano=E("0.30"), provider="p2"),
    )
    rig.settle("A", "0.30", T0 + 2, attempts=attempts)
    assert figures(rig.totals(w)) == (E("1.30"), 0, 0, 0)
    first = rig.run(rig.ledger.get_calls("A"))[0]
    assert (first.outcome, first.charged_nano) == (Outcome.UNREACHABLE, 0)


def test_scenario_18_only_candidate_unreachable_is_released(rig: Rig) -> None:
    w = rig.world("20")
    spend(rig, w, "1")
    rig.hold_ok(w, "A", "1")
    rig.release("A", attempts=(attempt(1, Outcome.UNREACHABLE, 0, T0, True),))
    assert figures(rig.totals(w)) == (E("1"), 0, 0, 0)  # unchanged


def test_scenario_19_5xx_after_send_ends_the_request_at_the_full_hold(rig: Rig) -> None:
    w = rig.world("20")
    spend(rig, w, "1.30")
    rig.hold_ok(w, "A", "1", providers=("p1", "p2"))
    hold = Money(E("1"), "EUR")
    assert charge(Outcome.UPSTREAM_5XX, None, hold) == hold  # d2 is not tried
    rig.settle("A", "1", outcome=Outcome.UPSTREAM_5XX)
    assert figures(rig.totals(w)) == (E("2.30"), 0, 0, 0)
    rows = rig.run(rig.ledger.get_calls("A"))
    assert [(r.attempt, r.outcome, r.charged_nano) for r in rows] == [
        (1, Outcome.UPSTREAM_5XX, E("1"))
    ]


def test_settle_rejects_charges_that_do_not_add_up(rig: Rig) -> None:
    w = rig.world("20")
    rig.hold_ok(w, "A", "1")
    with pytest.raises(ValueError, match="sum"):
        rig.run(rig.ledger.settle("A", E("1"), (attempt(1, Outcome.OK, E("0.5"), T0),), T0))
    with pytest.raises(ValueError, match="expired"):
        rig.settle("A", "1", outcome=Outcome.EXPIRED)


def test_a_release_before_any_attempt_writes_no_calls_and_frees_the_hold(rig: Rig) -> None:
    # e.g. every deployment was cooling, or the request could not be translated
    w = rig.world("20")
    rig.hold_ok(w, "A", "1")
    done = rig.release("A", attempts=())
    assert (done.applied, done.state) == (True, ReservationState.RELEASED)
    assert figures(rig.totals(w)) == (0, 0, 0, 0)
    assert rig.run(rig.ledger.get_calls("A")) == ()
