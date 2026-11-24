"""Budgets, grants, UTC periods and the admission check on one budget period row.

Pure maths over the numbers the ledger reads under its row lock. Time is epoch milliseconds
passed in as `now`; nothing here reads a clock.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from spendgate.core.errors import ErrorCode, LimitName, Refusal
from spendgate.core.money import Money

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MS = timedelta(milliseconds=1)
_SOFT_PCT_MIN, _SOFT_PCT_MAX = 1, 99


class Period(StrEnum):
    NONE = "none"
    DAY = "day"
    MONTH = "month"


class ScopeKind(StrEnum):
    ORG = "org"
    PROJECT = "project"
    SERVICE = "service"
    USER = "user"
    SESSION = "session"


@dataclass(frozen=True, slots=True)
class Grant:
    """Temporary extra room, active for `valid_from <= now < valid_until`."""

    amount_nano: int
    valid_from: int
    valid_until: int

    def __post_init__(self) -> None:
        if self.amount_nano <= 0 or self.valid_until <= self.valid_from:
            raise ValueError("a grant needs a positive amount and a non-empty window")

    def active(self, now: int) -> bool:
        return self.valid_from <= now < self.valid_until


@dataclass(frozen=True, slots=True)
class Budget:
    """A cap on a scope. There is no validity window: temporary room is a `Grant`."""

    id: str
    scope_id: str
    period: Period
    cap_nano: int
    soft_pct: int = 80
    model_filter: str | None = None
    strict_overrun: bool = True
    review_required_at: int | None = None
    scope_kind: ScopeKind = ScopeKind.SERVICE
    scope_path: str = ""
    grants: tuple[Grant, ...] = ()

    def __post_init__(self) -> None:
        if self.cap_nano < 0:
            raise ValueError("cap_nano must not be negative")
        if not _SOFT_PCT_MIN <= self.soft_pct <= _SOFT_PCT_MAX:
            raise ValueError("soft_pct must be between 1 and 99")


@dataclass(frozen=True, slots=True)
class PeriodState:
    """One `budget_periods` row."""

    spent_nano: int
    held_nano: int
    overrun_nano: int = 0
    excess_nano: int = 0
    soft_alerted: bool = False


@dataclass(frozen=True, slots=True)
class SoftWarning:
    """Event, never a refusal: usage crossed the budget's soft threshold."""

    budget_id: str
    soft_pct: int
    used_nano: int
    effective_cap_nano: int


class AlertKind(StrEnum):
    """Budget transitions worth an operator warning (operations.md, "Budget warnings")."""

    SOFT_THRESHOLD = "soft_threshold"  # usage crossed `soft_pct` of the effective cap
    CAP_BREACHED = "cap_breached"  # an overrun took `spent + held` above the effective cap
    BLOCKED = "blocked"  # that breach flagged a `strict_overrun` budget for review


@dataclass(frozen=True, slots=True)
class BudgetAlert:
    """One transition of one budget period, reported by the ledger in the transaction that made it.

    The ledger reports each transition once: a repeat of the same call reports nothing.
    """

    kind: AlertKind
    budget_id: str
    scope_path: str
    period: Period
    period_start: int
    spent_nano: int  # spent + held at the transition: what the threshold or cap is compared to
    cap_nano: int  # the effective cap (cap plus active grants)


@dataclass(frozen=True, slots=True)
class Admission:
    refusal: Refusal | None
    excess_nano: int  # to record even when refusing (normalisation is its own transaction)
    warning: SoftWarning | None = None


def period_start(period: Period, now: int) -> int:
    """Epoch ms of the UTC day/month start containing `now`; 0 for `none`."""
    if period is Period.NONE:
        return 0
    moment = _EPOCH + now * _MS
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if period is Period.MONTH:
        start = start.replace(day=1)
    return (start - _EPOCH) // _MS


def next_period_start(period: Period, now: int) -> int | None:
    """Epoch ms when the current period ends; None for `none`."""
    if period is Period.NONE:
        return None
    start = _EPOCH + period_start(period, now) * _MS
    if period is Period.DAY:
        end = start + timedelta(days=1)
    else:
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (end - _EPOCH) // _MS


def effective_cap(budget: Budget, now: int) -> int:
    """`cap_nano` plus every grant active at `now`."""
    return budget.cap_nano + sum(g.amount_nano for g in budget.grants if g.active(now))


def normalise_excess(state: PeriodState, effective: int, *, allow_shrink: bool = False) -> int:
    """`excess = max(excess, S + H - eff - ovr)`; on a cap change it may also shrink.

    Excess is what a lowered cap or an expired grant left above the cap. It is not an
    overrun, so I1 (`S + H <= eff + ovr + exc`) holds once this has run.
    """
    needed = max(0, state.spent_nano + state.held_nano - effective - state.overrun_nano)
    return needed if allow_shrink else max(state.excess_nano, needed)


def invariant_holds(state: PeriodState, effective: int) -> bool:
    """I1: spent + held <= effective cap + overrun + excess."""
    used = state.spent_nano + state.held_nano
    return used <= effective + state.overrun_nano + state.excess_nano


def _rfc3339(ms: int) -> str:
    return (_EPOCH + ms * _MS).strftime("%Y-%m-%dT%H:%M:%SZ")


def _exhausted_message(scope: str, remaining: Money, hold: Money) -> str:
    if remaining.nano == 0:
        return f"Budget for {scope} is exhausted."
    # Room is left, so the worst-case hold is what does not fit (often the catalog output
    # ceiling, used when the call sets no max_tokens). Say so: "exhausted" with money left
    # gives the caller nothing to change.
    return (
        f"The worst-case hold for this call ({hold.format()}) exceeds the remaining budget for "
        f"{scope} ({remaining.format()}). Set or lower max_tokens (or max_output_tokens) to "
        "lower the hold, or raise the cap."
    )


def _refusal(budget: Budget, remaining: Money, hold: Money, now: int, *, review: bool) -> Refusal:
    scope = budget.scope_path or budget.scope_id
    details: dict[str, str | int] = {"scope": scope, "remaining": remaining.format()}
    if budget.scope_kind is ScopeKind.SESSION:
        details["limit"] = LimitName.BUDGET.value
        return Refusal(ErrorCode.SESSION_LIMIT_REACHED, "Session budget reached.", None, details)
    if review:
        details["reason"] = "overrun_review"
        return Refusal(
            ErrorCode.BUDGET_EXHAUSTED, f"Budget for {scope} awaits overrun review.", None, details
        )
    details["hold"] = hold.format()
    message = _exhausted_message(scope, remaining, hold)
    ends = next_period_start(budget.period, now)
    if ends is None:
        return Refusal(ErrorCode.BUDGET_EXHAUSTED, message, None, details)
    details["period_resets_at"] = _rfc3339(ends)
    wait = -((now - ends) // 1000)  # ceiling of (ends - now) / 1000
    return Refusal(ErrorCode.BUDGET_PERIOD_EXHAUSTED, message, None, details, wait)


def _crossed_soft(budget: Budget, state: PeriodState, used: int, effective: int) -> bool:
    return not state.soft_alerted and used * 100 >= effective * budget.soft_pct


def check_admission(
    budget: Budget, state: PeriodState, worst_nano: int, now: int, currency: str
) -> Admission:
    """May a hold of `worst_nano` enter this budget period? (admission step 4)

    Refuses while an overrun review is pending, or when `S + H + worst > eff`. A soft-threshold
    crossing comes back as a warning on an admitted call.
    """
    effective = effective_cap(budget, now)
    excess = normalise_excess(state, effective)
    used = state.spent_nano + state.held_nano
    remaining = Money(max(0, effective - used), currency)
    hold = Money(worst_nano, currency)
    if budget.review_required_at is not None:
        return Admission(_refusal(budget, remaining, hold, now, review=True), excess)
    if used + worst_nano > effective:
        return Admission(_refusal(budget, remaining, hold, now, review=False), excess)
    warning = None
    if _crossed_soft(budget, state, used + worst_nano, effective):
        warning = SoftWarning(budget.id, budget.soft_pct, used + worst_nano, effective)
    return Admission(None, excess, warning)
