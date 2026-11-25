"""Periods, grants, excess and the admission check on one budget period (rules M-06..M-08)."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from spendgate.core.budgets import (
    Budget,
    Grant,
    Period,
    PeriodState,
    ScopeKind,
    check_admission,
    effective_cap,
    invariant_holds,
    next_period_start,
    normalise_excess,
    period_start,
)
from spendgate.core.errors import ErrorCode

CUR = "EUR"
U = 1_000  # one "unit" in these tests; the maths does not care about the scale


def ms(y: int, mo: int, d: int, *clock: int) -> int:
    """Epoch ms of a UTC date plus optional hour, minute, second, millisecond."""
    h, mi, s, milli = (*clock, 0, 0, 0, 0)[:4]
    moment = datetime(y, mo, d, h, mi, s, milli * 1000, tzinfo=UTC)
    return int(moment.timestamp() * 1000)


def budget(cap: int = 10 * U, **kw: object) -> Budget:
    base = Budget(id="b1", scope_id="s1", period=Period.NONE, cap_nano=cap)
    return replace(base, **kw)  # type: ignore[arg-type]


# --- periods ---------------------------------------------------------------------------------


def test_period_none_is_lifetime() -> None:
    assert period_start(Period.NONE, ms(2026, 11, 13, 12)) == 0
    assert next_period_start(Period.NONE, ms(2026, 11, 13)) is None


def test_period_day_starts_at_utc_midnight() -> None:
    assert period_start(Period.DAY, ms(2026, 11, 13, 17, 45, 1, 5)) == ms(2026, 11, 13)
    assert next_period_start(Period.DAY, ms(2026, 11, 13, 17)) == ms(2026, 11, 14)


def test_period_month_starts_on_the_first() -> None:
    assert period_start(Period.MONTH, ms(2026, 11, 30, 23, 59)) == ms(2026, 11, 1)
    assert next_period_start(Period.MONTH, ms(2026, 11, 13)) == ms(2026, 12, 1)
    assert next_period_start(Period.MONTH, ms(2026, 12, 31, 23)) == ms(2027, 1, 1)
    assert next_period_start(Period.MONTH, ms(2028, 2, 29)) == ms(2028, 3, 1)


# T-CLK-02
def test_period_rollover_at_23_59_59_999_utc() -> None:
    last = ms(2026, 11, 30, 23, 59, 59, 999)
    assert period_start(Period.DAY, last) == ms(2026, 11, 30)
    assert period_start(Period.DAY, last + 1) == ms(2026, 12, 1)
    assert period_start(Period.MONTH, last) == ms(2026, 11, 1)
    assert period_start(Period.MONTH, last + 1) == ms(2026, 12, 1)
    assert next_period_start(Period.DAY, last) == last + 1


def test_a_hold_keeps_its_period_start_across_the_rollover() -> None:
    held_at = ms(2026, 11, 5, 23, 59, 58)
    row = period_start(Period.DAY, held_at)
    assert period_start(Period.DAY, held_at + 3_000) != row


# --- effective cap, grants -------------------------------------------------------------------


def test_m06_effective_cap_is_cap_plus_active_grants() -> None:
    b = budget(10 * U, grants=(Grant(5 * U, 100, 1000), Grant(2 * U, 500, 600)))
    assert effective_cap(b, 99) == 10 * U
    assert effective_cap(b, 100) == 15 * U  # valid_from is inclusive
    assert effective_cap(b, 500) == 17 * U
    assert effective_cap(b, 600) == 15 * U  # valid_until is exclusive
    assert effective_cap(b, 1000) == 10 * U


def test_grant_validation() -> None:
    with pytest.raises(ValueError, match="grant"):
        Grant(0, 1, 2)
    with pytest.raises(ValueError, match="grant"):
        Grant(1, 5, 5)


def test_budget_validation() -> None:
    with pytest.raises(ValueError, match="cap"):
        budget(-1)
    for pct in (0, 100):
        with pytest.raises(ValueError, match="soft_pct"):
            budget(soft_pct=pct)


# --- admission -------------------------------------------------------------------------------


def test_admission_allows_a_hold_that_fits() -> None:
    result = check_admission(budget(), PeriodState(9 * U, 0), 1 * U, 0, CUR)
    assert result.refusal is None and result.excess_nano == 0


def test_m01_a_second_call_on_the_last_unit_is_refused_because_of_the_held_amount() -> None:
    state = PeriodState(9 * U, 1 * U)  # call A's hold is in place
    refusal = check_admission(budget(), state, 1 * U, 0, CUR).refusal
    assert refusal is not None
    assert refusal.code is ErrorCode.BUDGET_EXHAUSTED
    assert refusal.details["remaining"] == "0.000000000 EUR"


def test_refusal_with_room_left_says_the_worst_case_hold_is_too_big() -> None:
    """Issue #16: 9.999 left and a 10 hold is not "exhausted"; the message names the hold."""
    refusal = check_admission(budget(), PeriodState(1, 0), 10 * U, 0, CUR).refusal
    assert refusal is not None
    assert refusal.code is ErrorCode.BUDGET_EXHAUSTED and refusal.http_status == 402
    assert refusal.details["remaining"] == "0.000009999 EUR"
    assert refusal.details["hold"] == "0.000010000 EUR"
    assert "is exhausted" not in refusal.message
    assert "worst-case hold for this call (0.000010000 EUR)" in refusal.message
    assert "(0.000009999 EUR)" in refusal.message
    assert "max_tokens" in refusal.message and "raise the cap" in refusal.message


def test_period_refusal_with_room_left_says_the_worst_case_hold_is_too_big() -> None:
    b = budget(period=Period.MONTH, scope_path="org:acme")
    refusal = check_admission(b, PeriodState(0, 5 * U), 6 * U, ms(2026, 11, 13), CUR).refusal
    assert refusal is not None
    assert refusal.code is ErrorCode.BUDGET_PERIOD_EXHAUSTED
    assert refusal.details["hold"] == "0.000006000 EUR"
    assert refusal.details["period_resets_at"] == "2026-12-01T00:00:00Z"
    assert "worst-case hold for this call (0.000006000 EUR)" in refusal.message
    assert "org:acme (0.000005000 EUR)" in refusal.message


def test_refusal_with_nothing_left_keeps_the_exhausted_message() -> None:
    for period in (Period.NONE, Period.DAY):
        b = budget(period=period, scope_path="org:acme")
        refusal = check_admission(b, PeriodState(10 * U, 0), 1, ms(2026, 11, 13), CUR).refusal
        assert refusal is not None
        assert refusal.message == "Budget for org:acme is exhausted."
        assert refusal.details["remaining"] == "0.000000000 EUR"
        assert refusal.details["hold"] == "0.000000001 EUR"


def test_overrun_review_refusal_keeps_its_message_without_a_hold() -> None:
    refusal = check_admission(budget(review_required_at=5), PeriodState(0, 0), 1, 10, CUR).refusal
    assert refusal is not None
    assert refusal.message == "Budget for s1 awaits overrun review."
    assert "hold" not in refusal.details


def test_period_none_refusal_is_budget_exhausted_without_retry_after() -> None:
    refusal = check_admission(budget(), PeriodState(10 * U, 0), 1, 0, CUR).refusal
    assert refusal is not None
    assert refusal.code is ErrorCode.BUDGET_EXHAUSTED
    assert refusal.retry_after_seconds is None
    assert "period_resets_at" not in refusal.details


def test_period_budget_refusal_names_the_reset_and_retry_after() -> None:
    b = budget(period=Period.DAY, scope_path="org:acme/project:app")
    now = ms(2026, 11, 13, 23, 59, 59, 500)
    refusal = check_admission(b, PeriodState(10 * U, 0), 1, now, CUR).refusal
    assert refusal is not None
    assert refusal.code is ErrorCode.BUDGET_PERIOD_EXHAUSTED
    assert refusal.details["period_resets_at"] == "2026-11-14T00:00:00Z"
    assert refusal.details["scope"] == "org:acme/project:app"
    assert refusal.retry_after_seconds == 1  # ceiling of 0.5 s
    assert refusal.http_status == 402


def test_overrun_review_blocks_even_with_room_and_names_the_reason() -> None:
    b = budget(review_required_at=5)
    refusal = check_admission(b, PeriodState(0, 0), 1, 10, CUR).refusal
    assert refusal is not None
    assert refusal.code is ErrorCode.BUDGET_EXHAUSTED
    assert refusal.details["reason"] == "overrun_review"


def test_l_a_a_session_own_budget_refusal_is_session_limit_budget() -> None:
    b = budget(scope_kind=ScopeKind.SESSION)
    refusal = check_admission(b, PeriodState(10 * U, 0), 1, 0, CUR).refusal
    assert refusal is not None
    assert refusal.code is ErrorCode.SESSION_LIMIT_REACHED
    assert refusal.details["limit"] == "budget"


def test_soft_threshold_is_a_warning_never_a_refusal_and_fires_once() -> None:
    b = budget(soft_pct=80)
    below = check_admission(b, PeriodState(7 * U, 0), 1 * U - 1, 0, CUR)
    assert below.warning is None
    crossing = check_admission(b, PeriodState(7 * U, 0), 1 * U, 0, CUR)
    assert crossing.refusal is None and crossing.warning is not None
    assert (crossing.warning.soft_pct, crossing.warning.effective_cap_nano) == (80, 10 * U)
    again = check_admission(b, PeriodState(7 * U, 0, soft_alerted=True), 1 * U, 0, CUR)
    assert again.refusal is None and again.warning is None


def test_m06_t_mon_04_grant_added_then_expired_records_excess() -> None:
    grant = Grant(5 * U, 0, 1000)
    b = budget(grants=(grant,))
    # While the grant is active: eff 15, S 13.5 + H 1.0 fits, no excess.
    state = PeriodState(13_500, 1_000)
    assert check_admission(b, state, 0, 999, CUR).excess_nano == 0
    # After expiry: eff 10; normalisation records 14.5 - 10 = 4.5, then admission refuses.
    after = check_admission(b, state, 10, 1500, CUR)
    assert after.excess_nano == 4_500
    assert after.refusal is not None
    state = replace(state, spent_nano=14_100, held_nano=0, excess_nano=after.excess_nano)
    assert invariant_holds(state, effective_cap(b, 1500))


def test_m08_lowering_then_raising_a_cap_recomputes_excess() -> None:
    state = PeriodState(8_600, 0, excess_nano=4_000)
    assert normalise_excess(state, 5_000) == 4_000  # lazy path never shrinks
    assert normalise_excess(state, 5_000, allow_shrink=True) == 3_600
    assert normalise_excess(state, 12_000, allow_shrink=True) == 0
    assert normalise_excess(PeriodState(8_600, 1_000), 5_000, allow_shrink=True) == 4_600


def test_m07_overrun_inside_the_cap_does_not_block() -> None:
    state = PeriodState(6_500, 0, overrun_nano=500)
    assert check_admission(budget(), state, 1 * U, 0, CUR).refusal is None


# --- property: the invariant survives any sequence -------------------------------------------

OPS = st.lists(
    st.tuples(st.sampled_from(["admit", "settle", "tick", "lower", "raise"]), st.integers(0, 600)),
    max_size=80,
)


@given(
    ops=OPS,
    cap=st.integers(0, 2_000),
    grants=st.lists(
        st.tuples(st.integers(1, 1_000), st.integers(0, 500), st.integers(1, 800)), max_size=4
    ),
)
def test_m06_spent_plus_held_never_exceeds_cap_overrun_and_excess(
    ops: list[tuple[str, int]], cap: int, grants: list[tuple[int, int, int]]
) -> None:
    gs = tuple(Grant(a, start, start + length) for a, start, length in grants)
    b = budget(cap, grants=gs)
    now, spent, held, overrun, excess = 0, 0, 0, 0, 0
    holds: list[int] = []
    for kind, value in ops:
        if kind == "admit":
            state = PeriodState(spent, held, overrun, excess)
            result = check_admission(b, state, value, now, CUR)
            excess = result.excess_nano
            if result.refusal is None:
                assert spent + held + value <= effective_cap(b, now)
                held += value
                holds.append(value)
        elif kind == "settle" and holds:
            hold = holds.pop(0)
            charge = value  # may exceed the hold: booked in full, never clamped
            held -= hold
            spent += charge
            overrun += max(0, charge - hold)
        elif kind == "tick":
            now += value
            excess = normalise_excess(
                PeriodState(spent, held, overrun, excess), effective_cap(b, now)
            )
        elif kind == "lower":
            b = replace(b, cap_nano=b.cap_nano // 2)
            state = PeriodState(spent, held, overrun, excess)
            excess = normalise_excess(state, effective_cap(b, now), allow_shrink=True)
        elif kind == "raise":
            b = replace(b, cap_nano=b.cap_nano + value)
            state = PeriodState(spent, held, overrun, excess)
            excess = normalise_excess(state, effective_cap(b, now), allow_shrink=True)
        state = PeriodState(spent, held, overrun, excess)
        assert invariant_holds(state, effective_cap(b, now))
        assert spent >= 0 and held >= 0


@given(
    st.integers(0, 10**6),
    st.integers(0, 10**6),
    st.integers(0, 10**6),
    st.integers(0, 10**6),
    st.integers(0, 10**6),
)
def test_excess_is_never_negative_and_makes_i1_true(
    spent: int, held: int, eff: int, ovr: int, exc: int
) -> None:
    state = PeriodState(spent, held, ovr, exc)
    for shrink in (False, True):
        new = normalise_excess(state, eff, allow_shrink=shrink)
        assert new >= 0
        assert invariant_holds(replace(state, excess_nano=new), eff)
    assert normalise_excess(state, eff) >= exc
