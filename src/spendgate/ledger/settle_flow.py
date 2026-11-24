"""Settle, release, expire and renew: the transitions after `hold` (ledger-state-machine.md).

Every transition starts with the state-guarded UPDATE on the reservation row; 0 rows means a
terminal transition already happened, which makes each one idempotent per request id.
"""

from spendgate.core.budgets import AlertKind, Budget, BudgetAlert, effective_cap
from spendgate.core.outcomes import Outcome, ReservationState
from spendgate.ledger import sql
from spendgate.ledger.common import (
    attempt_params,
    budget_alert,
    call_params,
    i,
    lock_budget,
    s,
)
from spendgate.ledger.port import AttemptRecord, HoldMeta, NotFoundError, Settlement
from spendgate.ledger.sql import Flow, Row, q


def _check_attempts(charge: int, attempts: tuple[AttemptRecord, ...]) -> None:
    if not attempts or charge < 0:
        raise ValueError("settle needs at least one attempt and a non-negative charge")
    if sum(a.charged_nano for a in attempts) != charge:
        raise ValueError("per-attempt charges must sum to the settle charge")
    if any(a.outcome is Outcome.EXPIRED for a in attempts):
        raise ValueError("only the sweeper writes the expired outcome")


def settle_flow(
    request_id: str, charge: int, attempts: tuple[AttemptRecord, ...], now: int
) -> Flow[Settlement]:
    """Move the money once: `H -= held; S += charge`; the part above the hold is an overrun."""
    _check_attempts(charge, attempts)
    rows = yield q(
        sql.RES_SETTLE,
        id=request_id,
        outcome=attempts[-1].outcome.value,
        charged=charge,
        now=now,
    )
    if not rows:
        state = yield from _state_of(request_id)
        if state is ReservationState.EXPIRED:
            return (yield from _late_settle(request_id, charge, attempts, now))
        return Settlement(False, state)
    items = yield q(sql.ITEMS_OF, id=request_id)
    held = max((i(it, "held_nano") for it in items), default=0)
    over = max(0, charge - held)
    alerts: list[BudgetAlert] = []
    for item in items:  # ascending budget id
        budget = yield from _locked(item)
        start = i(item, "period_start")
        yield q(
            sql.PERIOD_BOOK,
            budget_id=budget.id,
            period_start=start,
            held=i(item, "held_nano"),
            charge=charge,
            over=over,
        )
        if over:
            yield q(sql.ITEM_OVERRUN, id=request_id, budget_id=budget.id, over=over)
            added = charge - i(item, "held_nano")  # what this booking added to spent + held
            alerts += yield from _breach(budget, start, added, now)
    yield from _insert_calls(rows[0], request_id, attempts, held, over)
    return Settlement(
        True, ReservationState.SETTLED, charge, over, review_flagged=_blocked(alerts),
        alerts=tuple(alerts),
    )  # fmt: skip


def _insert_calls(
    res: Row, request_id: str, attempts: tuple[AttemptRecord, ...], held: int, over: int
) -> Flow[None]:
    """One calls row per attempt; the overrun is recorded on the attempt that used the hold."""
    if not attempts:  # a release before any attempt was made writes no calls rows
        return
    meta = HoldMeta.from_json(s(res, "meta"))
    used = _consuming(attempts)
    for a in attempts:
        yield q(
            sql.CALL_INSERT,
            **call_params(
                res,
                meta,
                a,
                hold=held,
                overrun=over if a is used else 0,
                charged=a.charged_nano,
                settled_by="owner",
                request_id=request_id,
            ),
        )


def _locked(item: Row) -> Flow[Budget]:
    """Lock the budget of a reservation item; it cannot have been deleted while held."""
    budget = yield from lock_budget(s(item, "budget_id"))
    if budget is None:
        raise NotFoundError(f"budget {item['budget_id']} is gone")
    return budget


def _state_of(request_id: str) -> Flow[ReservationState]:
    rows = yield q(sql.RES_GET, id=request_id)
    if not rows:
        raise NotFoundError(f"reservation {request_id} does not exist")
    return ReservationState(s(rows[0], "state"))


def _breach(budget: Budget, start: int, added: int, now: int) -> Flow[list[BudgetAlert]]:
    """An overrun that leaves `S + H` above the effective cap: alert, and flag a strict budget.

    `cap_breached` is reported only by the booking that crossed the cap (`S + H` was within it
    before `added`): once per crossing. Overruns while above the cap do not repeat it; a period
    that dips back within the cap and crosses again reports a new crossing. `blocked` follows
    `BUDGET_FLAG`, which sets `review_required_at` only once until `ack-overrun`.
    """
    rows = yield q(sql.PERIOD_GET, budget_id=budget.id, period_start=start)
    used = i(rows[0], "spent_nano") + i(rows[0], "held_nano")
    cap = effective_cap(budget, now)
    if used <= cap:
        return []
    kinds: list[AlertKind] = []
    if used - added <= cap:
        kinds.append(AlertKind.CAP_BREACHED)
    if budget.strict_overrun and (yield q(sql.BUDGET_FLAG, id=budget.id, now=now)):
        kinds.append(AlertKind.BLOCKED)
    alerts: list[BudgetAlert] = []
    for kind in kinds:
        alerts.append((yield from budget_alert(kind, budget, start, used, cap)))
    return alerts


def _blocked(alerts: list[BudgetAlert]) -> tuple[str, ...]:
    return tuple(a.budget_id for a in alerts if a.kind is AlertKind.BLOCKED)


def _consuming(attempts: tuple[AttemptRecord, ...]) -> AttemptRecord:
    """The attempt that used the hold: the last one that may have been processed."""
    return next((a for a in reversed(attempts) if not a.not_processed), attempts[-1])


def _late_settle(
    request_id: str, charge: int, attempts: tuple[AttemptRecord, ...], now: int
) -> Flow[Settlement]:
    """The sweeper already charged the full hold; book only `max(0, charge - held)` (M-04).

    The sweeper's `calls` row is rewritten as the attempt that consumed the hold; its
    `outcome = 'expired'` marks "not yet late-settled", so a repeat finds nothing to do. The
    reservation's `charged_nano` follows the same `max(held, charge)` as that row.
    """
    items = yield q(sql.ITEMS_OF, id=request_id)
    held = max((i(it, "held_nano") for it in items), default=0)
    over = max(0, charge - held)
    swept = yield q(sql.CALL_SWEEPER_ROW, id=request_id)
    if not swept:
        return Settlement(False, ReservationState.EXPIRED)
    used = _consuming(attempts)
    updated = yield q(
        sql.CALL_LATE,
        request_id=request_id,
        old_attempt=i(swept[0], "attempt"),
        **attempt_params(used, charged=max(held, charge), overrun=over),
    )
    if not updated:  # another late settle won the race
        return Settlement(False, ReservationState.EXPIRED)
    yield q(sql.RES_LATE, id=request_id, charged=max(held, charge))
    alerts = yield from _book_late(request_id, items if over else [], over, now)
    res = (yield q(sql.RES_GET, id=request_id))[0]
    meta = HoldMeta.from_json(s(res, "meta"))
    for a in attempts:
        if a is not used:  # the other attempts get their own rows, charged 0 (money is on `used`)
            yield q(
                sql.CALL_INSERT,
                **call_params(
                    res,
                    meta,
                    a,
                    hold=held,
                    overrun=0,
                    charged=0,
                    settled_by="owner",
                    request_id=request_id,
                ),
            )
    return Settlement(
        True, ReservationState.EXPIRED, over, over, late=True, review_flagged=_blocked(alerts),
        alerts=tuple(alerts),
    )  # fmt: skip


def _book_late(request_id: str, items: list[Row], over: int, now: int) -> Flow[list[BudgetAlert]]:
    """Add only the overrun to spent (the hold was already charged in full by the sweeper)."""
    alerts: list[BudgetAlert] = []
    for item in items:
        budget = yield from _locked(item)
        start = i(item, "period_start")
        yield q(sql.PERIOD_LATE, budget_id=budget.id, period_start=start, over=over)
        yield q(sql.ITEM_OVERRUN, id=request_id, budget_id=budget.id, over=over)
        alerts += yield from _breach(budget, start, over, now)
    return alerts


def release_flow(
    request_id: str, attempts: tuple[AttemptRecord, ...], now: int
) -> Flow[Settlement]:
    """Final refund: only when every attempt was `not_processed` (never sent, or a free status)."""
    if any(not a.not_processed or a.charged_nano != 0 for a in attempts):
        raise ValueError("release needs every attempt not_processed and charged 0")
    outcome = attempts[-1].outcome.value if attempts else None
    rows = yield q(sql.RES_RELEASE, id=request_id, outcome=outcome, now=now)
    if not rows:
        return Settlement(False, (yield from _state_of(request_id)))
    res = rows[0]
    items = yield q(sql.ITEMS_OF, id=request_id)
    for item in items:
        budget = yield from _locked(item)
        yield q(
            sql.PERIOD_RELEASE,
            budget_id=budget.id,
            period_start=i(item, "period_start"),
            held=i(item, "held_nano"),
        )
    if res["session_id"] is not None:
        yield q(sql.SESSION_REFUND, id=s(res, "session_id"))  # the step is refunded, session stays
    held = max((i(it, "held_nano") for it in items), default=0)
    yield from _insert_calls(res, request_id, attempts, held, 0)
    return Settlement(True, ReservationState.RELEASED)


def expire_flow(request_id: str, now: int) -> Flow[bool]:
    """Sweeper: charge the whole hold of one lease-expired reservation. True if it did."""
    rows = yield q(sql.RES_EXPIRE, id=request_id, now=now)
    if not rows:
        return False  # the owner settled first, or someone else swept it
    res = rows[0]
    items = yield q(sql.ITEMS_OF, id=request_id)
    held = max((i(it, "held_nano") for it in items), default=0)
    for item in items:
        budget = yield from _locked(item)
        yield q(
            sql.PERIOD_BOOK,
            budget_id=budget.id,
            period_start=i(item, "period_start"),
            held=i(item, "held_nano"),
            charge=i(item, "held_nano"),
            over=0,
        )
    meta = HoldMeta.from_json(s(res, "meta"))
    guess = meta.candidates[0]  # which deployment was in use is unknown after a crash
    a = AttemptRecord(
        1,
        guess.provider,
        guess.deployment,
        guess.price_id,
        Outcome.EXPIRED,
        held,
        i(res, "created_at"),
        now,
        native_currency=guess.native_currency,
        fx_rate_nano=guess.fx_rate_nano,
    )
    yield q(
        sql.CALL_INSERT,
        **call_params(
            res,
            meta,
            a,
            hold=held,
            overrun=0,
            charged=held,
            settled_by="sweeper",
            request_id=request_id,
        ),
    )
    return True


def due_flow(now: int, limit: int) -> Flow[list[str]]:
    rows = yield q(sql.RES_DUE, now=now, limit=limit)
    return [s(r, "id") for r in rows]


def renew_flow(instance: str, ids: tuple[str, ...], until: int) -> Flow[tuple[str, ...]]:
    """Extend the lease of each named hold that is still pending and owned by this instance.

    Only the ids the caller still owns: a hold whose owner failed to settle must reach the sweeper.
    """
    renewed: list[str] = []
    for rid in ids:
        rows = yield q(sql.RES_RENEW, id=rid, owner=instance, until=until)
        renewed.extend(s(r, "id") for r in rows)
    return tuple(sorted(renewed))
