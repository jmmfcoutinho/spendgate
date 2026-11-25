"""Pieces every flow uses: row accessors, budget loading under lock, normalisation, call rows."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from spendgate.core.budgets import (
    AlertKind,
    Budget,
    BudgetAlert,
    Grant,
    Period,
    PeriodState,
    ScopeKind,
    effective_cap,
    normalise_excess,
    period_start,
)
from spendgate.ledger import sql
from spendgate.ledger.port import AttemptRecord, HoldMeta
from spendgate.ledger.sql import Flow, Row, Value, q

HOUR_MS: Final = 3_600_000


@dataclass(frozen=True, slots=True)
class Ctx:
    """What a flow needs besides its arguments: the ledger currency and an id factory."""

    currency: str
    new_id: Callable[[], str]


def i(row: Row, key: str) -> int:
    value = row[key]
    if not isinstance(value, int):
        raise TypeError(f"{key} is not an integer: {value!r}")
    return value


def s(row: Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} is not text: {value!r}")
    return value


def oi(row: Row, key: str) -> int | None:
    value = row[key]
    if value is not None and not isinstance(value, int):
        raise TypeError(f"{key} is not an integer: {value!r}")
    return value


def audit(  # noqa: PLR0913, PLR0917 - one audit row needs all of these
    ctx: Ctx,
    now: int,
    actor: str,
    action: str,
    target: tuple[str, str],
    before: dict[str, Value] | None,
    after: dict[str, Value] | None,
) -> Flow[None]:
    """Admin changes write their audit row in the same transaction (append-only table)."""
    yield q(
        sql.AUDIT_INSERT,
        id=ctx.new_id(),
        now=now,
        actor=actor,
        action=action,
        target_type=target[0],
        target_id=target[1],
        before=None if before is None else json.dumps(before, sort_keys=True),
        after=None if after is None else json.dumps(after, sort_keys=True),
    )


def lock_budget(
    budget_id: str, scope: tuple[ScopeKind, str] | None = None, *, skip_disabled: bool = False
) -> Flow[Budget | None]:
    """Lock the budget row and read it with its grants. None if missing (or disabled and skipped).

    Everything that touches a budget's money calls this first, in ascending id order.
    """
    rows = yield q(sql.BUDGET_LOCK, id=budget_id)
    if not rows or (skip_disabled and rows[0]["disabled_at"] is not None):
        return None
    grants = yield q(sql.GRANTS_OF, id=budget_id)
    return budget_from(rows[0], grants, scope)


def read_budget(budget_id: str) -> Flow[Budget | None]:
    """The budget with its grants, without a lock: for reports, never for a money decision."""
    rows = yield q(sql.BUDGET_GET, id=budget_id)
    return budget_from(rows[0], (yield q(sql.GRANTS_OF, id=budget_id))) if rows else None


def budget_from(row: Row, grants: list[Row], scope: tuple[ScopeKind, str] | None = None) -> Budget:
    """A budget row and its grant rows as a `Budget`; `scope` names its owner when known."""
    kind, path = scope if scope is not None else (ScopeKind.SERVICE, "")
    return Budget(
        id=s(row, "id"),
        scope_id=s(row, "scope_id"),
        period=Period(s(row, "period")),
        cap_nano=i(row, "cap_nano"),
        soft_pct=i(row, "soft_pct"),
        model_filter=row["model_filter"] if isinstance(row["model_filter"], str) else None,
        strict_overrun=bool(i(row, "strict_overrun")),
        review_required_at=oi(row, "review_required_at"),
        scope_kind=kind,
        scope_path=path,
        grants=tuple(
            Grant(i(g, "amount_nano"), i(g, "valid_from"), i(g, "valid_until")) for g in grants
        ),
    )


def budget_alert(
    kind: AlertKind, budget: Budget, start: int, spent: int, cap: int
) -> Flow[BudgetAlert]:
    """An alert for one budget period, named by its owning scope, or a session's parent scope.

    Settle locks budgets without their scope and a session path names the key and the caller's
    session id, so the scope is read here; alerts are rare, so the extra reads are cheap.
    """
    rows = yield q(sql.SCOPE_GET, id=budget.scope_id)
    owner = rows[0] if rows else None
    session = owner is not None and s(owner, "kind") == ScopeKind.SESSION.value
    if owner is None:  # a budget's scope is never deleted; fall back to its id, never a guess
        path = budget.scope_id
    elif session:  # a session always has a parent: the key's scope; never its own path
        parent = yield q(sql.SCOPE_GET, id=s(owner, "parent_id"))
        path = s(parent[0], "path") if parent else ""
    else:
        path = s(owner, "path")
    return BudgetAlert(kind, budget.id, path, budget.period, start, spent, cap, session)


def period_state(row: Row) -> PeriodState:
    return PeriodState(
        i(row, "spent_nano"),
        i(row, "held_nano"),
        i(row, "overrun_nano"),
        i(row, "excess_nano"),
        row.get("soft_alerted_at") is not None,
    )


def ensure_period(budget_id: str, start: int) -> Flow[PeriodState]:
    """Create the (budget, period) row lazily under the budget lock and read it."""
    yield q(sql.PERIOD_ENSURE, budget_id=budget_id, period_start=start)
    rows = yield q(sql.PERIOD_GET, budget_id=budget_id, period_start=start)
    return period_state(rows[0])


def normalise_budgets(budget_ids: list[str], now: int) -> Flow[list[str]]:
    """Record `excess` on the current period row of each budget, in ascending id order.

    This is `normalise_chain` (A1): its own transaction, so a refused admission cannot roll the
    recorded excess back. Returns the budgets whose excess changed.
    """
    changed: list[str] = []
    for budget_id in sorted(budget_ids):
        budget = yield from lock_budget(budget_id)
        if budget is None:
            continue
        start = period_start(budget.period, now)
        state = yield from ensure_period(budget_id, start)
        excess = normalise_excess(state, effective_cap(budget, now))
        if excess != state.excess_nano:
            yield q(sql.PERIOD_EXCESS, budget_id=budget_id, period_start=start, excess=excess)
            changed.append(budget_id)
    return changed


def renormalise_all_periods(budget: Budget, now: int) -> Flow[None]:
    """After a cap or grant change: `excess = max(0, S + H - eff - ovr)` on every period row.

    Unlike lazy normalisation this may also shrink the excess (a raised cap gives room back).
    """
    rows = yield q(sql.PERIODS_OF, budget_id=budget.id)
    eff = effective_cap(budget, now)
    for row in rows:
        excess = normalise_excess(period_state(row), eff, allow_shrink=True)
        if excess != i(row, "excess_nano"):
            start = i(row, "period_start")
            yield q(sql.PERIOD_EXCESS, budget_id=budget.id, period_start=start, excess=excess)


def call_params(  # noqa: PLR0913 - one calls row needs all of these
    reservation: Row,
    meta: HoldMeta,
    a: AttemptRecord,
    *,
    hold: int,
    overrun: int,
    charged: int,
    settled_by: str,
    request_id: str,
) -> dict[str, Value]:
    return {
        "request_id": request_id,
        "key_id": s(reservation, "key_id"),
        "scope_id": s(reservation, "scope_id"),
        "session_id": reservation["session_id"],
        "user_label": meta.user_label,
        "labels": json.dumps(dict(meta.labels), sort_keys=True),
        "endpoint": meta.endpoint,
        "model": meta.model,
        "catalog_sha": meta.catalog_sha,
        "hold_nano": hold,
        "settled_by": settled_by,
        **attempt_params(a, charged=charged, overrun=overrun),
    }


def attempt_params(a: AttemptRecord, *, charged: int, overrun: int) -> dict[str, Value]:
    """The per-attempt columns (also what a late settle writes over the sweeper's row)."""
    return {
        "attempt": a.attempt,
        "provider": a.provider,
        "deployment": a.deployment,
        "price_id": a.price_id,
        "native_currency": a.native_currency,
        "native_cost_nano": a.native_cost_nano,
        "fx_rate_nano": a.fx_rate_nano,
        "charged_nano": charged,
        "usage_cost_nano": a.usage_cost_nano,
        "overrun_nano": overrun,
        "http_status": a.http_status,
        "outcome": a.outcome.value,
        "error_code": a.error_code,
        "streamed": int(a.streamed),
        "review_required_at": a.finished_at if a.review_required else None,
        "started_at": a.started_at,
        "finished_at": a.finished_at,
        "latency_ms": a.latency_ms,
        "ttfb_ms": a.ttfb_ms,
        "tokens_in": a.usage.tokens_in,
        "tokens_out": a.usage.tokens_out,
        "tokens_cached": a.usage.tokens_cached,
        "tokens_cache_write": a.usage.tokens_cache_write,
        "tokens_reasoning": a.usage.tokens_reasoning,
        "audio_tokens_in": a.usage.audio_tokens_in,
        "audio_tokens_out": a.usage.audio_tokens_out,
        "audio_ms": a.usage.audio_ms,
        "characters": a.usage.characters,
        "images": a.usage.images,
    }
