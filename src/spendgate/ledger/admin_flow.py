"""Admin-side changes and reads, named after the admin API operations.

Every change writes its `admin_audit` row in the same transaction. Cap and grant changes also
re-normalise `excess` so the invariant `S + H <= eff + ovr + exc` holds when they commit.
"""

from dataclasses import replace

from spendgate.core.budgets import Budget, Period, ScopeKind, effective_cap, period_start
from spendgate.core.policy import KillSwitch
from spendgate.ledger import sql
from spendgate.ledger.common import (
    Ctx,
    audit,
    i,
    lock_budget,
    normalise_budgets,
    oi,
    renormalise_all_periods,
    s,
)
from spendgate.ledger.port import (
    BudgetTotals,
    ConflictError,
    CurrencyMismatch,
    NotFoundError,
    ScopeRow,
)
from spendgate.ledger.sql import Flow, Row, q

_RANK = {k: n for n, k in enumerate(ScopeKind)}


def create_scope_flow(  # noqa: PLR0913 - mirrors the admin ScopeCreate body
    ctx: Ctx,
    kind: ScopeKind,
    name: str,
    parent_id: str | None,
    now: int,
    *,
    actor: str = "system",
    display_name: str | None = None,
) -> Flow[ScopeRow]:
    if kind is ScopeKind.SESSION:
        raise ValueError("session scopes are created by the first call that uses them")
    parent_path = ""
    if parent_id is None:
        if kind is not ScopeKind.ORG:
            raise ValueError("only an org scope has no parent")
    else:
        rows = yield q(sql.SCOPE_GET, id=parent_id)
        if not rows:
            raise NotFoundError(f"scope {parent_id} does not exist")
        if _RANK[ScopeKind(s(rows[0], "kind"))] >= _RANK[kind]:
            raise ValueError("a scope must sit below its parent (org > project > service > user)")
        parent_path = s(rows[0], "path") + "/"
    scope_id = ctx.new_id()
    path = f"{parent_path}{kind.value}:{name}"
    if (yield q(sql.SCOPE_BY_PATH, path=path)):
        raise ConflictError(f"scope {path} already exists")
    yield q(
        sql.SCOPE_INSERT_NAMED,
        id=scope_id,
        kind=kind.value,
        parent_id=parent_id,
        name=name,
        path=path,
        now=now,
        display_name=display_name,
    )
    yield from audit(
        ctx, now, actor, "scope.create", ("scope", scope_id), None,
        {"path": path, "display_name": display_name},
    )  # fmt: skip
    return ScopeRow(
        scope_id, kind, parent_id, name, path, created_at=now, display_name=display_name
    )


def update_scope_flow(
    ctx: Ctx, scope_id: str, display_name: str | None, actor: str, now: int
) -> Flow[ScopeRow]:
    rows = yield q(sql.SCOPE_GET, id=scope_id)
    if not rows:
        raise NotFoundError(f"scope {scope_id} does not exist")
    yield q(sql.SCOPE_SET_NAME, id=scope_id, display_name=display_name)
    before = scope_row(rows[0])
    yield from audit(
        ctx, now, actor, "scope.update", ("scope", scope_id),
        {"display_name": before.display_name}, {"display_name": display_name},
    )  # fmt: skip
    return replace(before, display_name=display_name)


def _budget_json(b: Budget) -> dict[str, int | str | None]:
    return {
        "scope_id": b.scope_id,
        "period": b.period.value,
        "cap_nano": b.cap_nano,
        "soft_pct": b.soft_pct,
        "strict_overrun": int(b.strict_overrun),
    }


def create_budget_flow(  # noqa: PLR0913 - mirrors the admin BudgetCreate body
    ctx: Ctx,
    *,
    scope_id: str,
    period: Period,
    cap_nano: int,
    now: int,
    soft_pct: int,
    model_filter: str | None,
    strict_overrun: bool,
    note: str | None,
    actor: str,
    budget_id: str | None = None,
) -> Flow[Budget]:
    budget = Budget(
        budget_id or ctx.new_id(),
        scope_id,
        period,
        cap_nano,
        soft_pct,
        model_filter,
        strict_overrun,
    )  # validates cap and soft_pct
    if not (yield q(sql.SCOPE_GET, id=scope_id)):
        raise NotFoundError(f"scope {scope_id} does not exist")
    if budget_id is not None and (yield q(sql.BUDGET_NOTE, id=budget_id)):
        raise ConflictError(f"budget {budget_id} already exists")
    yield q(
        sql.BUDGET_INSERT,
        id=budget.id,
        scope_id=scope_id,
        period=period.value,
        cap=cap_nano,
        soft_pct=soft_pct,
        model_filter=model_filter,
        strict=int(strict_overrun),
        note=note,
        now=now,
    )
    yield from audit(
        ctx, now, actor, "budget.create", ("budget", budget.id), None, _budget_json(budget)
    )
    return budget


def update_budget_flow(  # noqa: PLR0913
    ctx: Ctx,
    *,
    budget_id: str,
    actor: str,
    now: int,
    cap_nano: int | None,
    soft_pct: int | None,
    strict_overrun: bool | None,
    note: str | None = None,
) -> Flow[Budget]:
    """Change the cap (even below spent: `excess` records it) and re-normalise (scenarios 7, 16)."""
    before = yield from must_lock(budget_id)
    if cap_nano is not None and cap_nano < 0:
        raise ValueError("cap_nano must not be negative")
    yield q(
        sql.BUDGET_UPDATE,
        id=budget_id,
        cap=cap_nano,
        soft_pct=soft_pct,
        strict=None if strict_overrun is None else int(strict_overrun),
    )
    if note is not None:
        yield q(sql.BUDGET_SET_NOTE, id=budget_id, note=note)
    after = yield from must_lock(budget_id)
    yield from renormalise_all_periods(after, now)
    yield from audit(
        ctx,
        now,
        actor,
        "budget.set_cap",
        ("budget", budget_id),
        _budget_json(before),
        _budget_json(after),
    )
    return after


def must_lock(budget_id: str) -> Flow[Budget]:
    budget = yield from lock_budget(budget_id)
    if budget is None:
        raise NotFoundError(f"budget {budget_id} does not exist")
    return budget


def create_grant_flow(  # noqa: PLR0913
    ctx: Ctx,
    *,
    budget_id: str,
    amount_nano: int,
    valid_from: int,
    valid_until: int,
    actor: str,
    reason: str,
    now: int,
) -> Flow[str]:
    yield from must_lock(budget_id)
    grant_id = ctx.new_id()
    yield q(
        sql.GRANT_INSERT,
        id=grant_id,
        budget_id=budget_id,
        amount=amount_nano,
        valid_from=valid_from,
        valid_until=valid_until,
        actor=actor,
        reason=reason,
        now=now,
    )
    after = yield from must_lock(budget_id)  # re-read: now includes the new grant
    yield from renormalise_all_periods(after, now)
    yield from audit(
        ctx,
        now,
        actor,
        "budget.grant",
        ("budget", budget_id),
        None,
        {"grant_id": grant_id, "amount_nano": amount_nano, "valid_until": valid_until},
    )
    return grant_id


def ack_overrun_flow(ctx: Ctx, budget_id: str, actor: str, reason: str, now: int) -> Flow[None]:
    budget = yield from must_lock(budget_id)
    yield q(sql.BUDGET_ACK, id=budget_id)
    yield from audit(
        ctx,
        now,
        actor,
        "overrun.ack",
        ("budget", budget_id),
        {"review_required_at": budget.review_required_at},
        {"reason": reason},
    )


def engage_kill_switch_flow(
    ctx: Ctx, switch: KillSwitch, actor: str, reason: str, now: int
) -> Flow[str]:
    switch_id = ctx.new_id()
    kind, ref = switch.target_kind.value, switch.target_ref
    yield q(sql.KILL_INSERT, id=switch_id, kind=kind, ref=ref, reason=reason, actor=actor, now=now)
    yield from audit(
        ctx, now, actor, "kill.set", ("kill_switch", switch_id), None, {"kind": kind, "ref": ref}
    )
    return switch_id


def lift_kill_switch_flow(ctx: Ctx, switch_id: str, actor: str, now: int) -> Flow[None]:
    if not (yield q(sql.KILL_GET, id=switch_id)):
        raise NotFoundError(f"kill switch {switch_id} does not exist")
    yield q(sql.KILL_LIFT, id=switch_id, now=now)
    yield from audit(ctx, now, actor, "kill.lift", ("kill_switch", switch_id), None, None)


def disable_scope_flow(ctx: Ctx, scope_id: str, actor: str, now: int) -> Flow[None]:
    """A disabled scope refuses every hold whose chain contains it (ADR-0003)."""
    if not (yield q(sql.SCOPE_GET, id=scope_id)):
        raise NotFoundError(f"scope {scope_id} does not exist")
    yield q(sql.SCOPE_DISABLE, id=scope_id, now=now)
    yield from audit(
        ctx, now, actor, "scope.disable", ("scope", scope_id), None, {"disabled_at": now}
    )


def delete_budget_flow(ctx: Ctx, budget_id: str, actor: str, now: int) -> Flow[None]:
    """Soft delete: the budget stops applying; history and in-flight holds stay settleable."""
    yield from must_lock(budget_id)
    yield q(sql.BUDGET_DISABLE, id=budget_id, now=now)
    yield from audit(
        ctx, now, actor, "budget.delete", ("budget", budget_id), None, {"disabled_at": now}
    )


def currency_flow(currency: str) -> Flow[None]:
    """Record the ledger currency on first open; refuse a different one afterwards."""
    yield q(sql.META_INIT, currency=currency)
    stored = s((yield q(sql.META_GET))[0], "value")
    if stored != currency:
        raise CurrencyMismatch(f"ledger currency is {stored}, opened with {currency}")


def add_key_record_flow(key_id: str, scope_id: str, now: int) -> Flow[None]:
    """Idempotent: a key id that exists (made by a racing process) is left as it is."""
    if (yield q(sql.KEY_GET, id=key_id)):
        return
    yield q(sql.KEY_INSERT, id=key_id, scope_id=scope_id, now=now)


def grant_expiry_flow(now: int) -> Flow[list[str]]:
    """Sweeper pass: record `excess` where a grant expired since the last touch."""
    rows = yield q(sql.GRANT_EXPIRED, now=now)
    return (yield from normalise_budgets([s(r, "budget_id") for r in rows], now))


# --- reads -----------------------------------------------------------------------------------


def budget_totals_flow(budget: Budget, now: int) -> Flow[BudgetTotals]:
    """One budget's current period row (zeros when nothing was spent) with its effective cap."""
    start = period_start(budget.period, now)
    found = yield q(sql.PERIOD_GET, budget_id=budget.id, period_start=start)
    p = found[0] if found else {}
    return BudgetTotals(
        budget.id,
        start,
        effective_cap(budget, now),
        _n(p, "spent_nano"),
        _n(p, "held_nano"),
        _n(p, "overrun_nano"),
        _n(p, "excess_nano"),
        _n(p, "calls_count"),
        budget.review_required_at,
    )


def totals_flow(scope_id: str, now: int) -> Flow[tuple[BudgetTotals, ...]]:
    out: list[BudgetTotals] = []
    for row in (yield q(sql.BUDGETS_OF_SCOPE, scope=scope_id)):
        budget = yield from must_lock(s(row, "id"))
        out.append((yield from budget_totals_flow(budget, now)))
    return tuple(out)


def _n(row: Row, key: str) -> int:
    return i(row, key) if row else 0


def scope_row(r: Row) -> ScopeRow:
    return ScopeRow(
        s(r, "id"),
        ScopeKind(s(r, "kind")),
        r["parent_id"] if isinstance(r["parent_id"], str) else None,
        s(r, "name"),
        s(r, "path"),
        i(r, "step_count"),
        oi(r, "max_steps"),
        oi(r, "started_at"),
        oi(r, "last_activity_at"),
        oi(r, "closed_at"),
        r["closed_reason"] if isinstance(r["closed_reason"], str) else None,
        i(r, "created_at"),
        r["display_name"] if isinstance(r["display_name"], str) else None,
        oi(r, "max_seconds"),
    )


def get_scope_flow(scope_id: str) -> Flow[ScopeRow | None]:
    rows = yield q(sql.SCOPE_GET, id=scope_id)
    return scope_row(rows[0]) if rows else None
