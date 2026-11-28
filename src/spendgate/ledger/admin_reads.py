"""Flows behind `AdminLedger`: lookups, listings and the key-expiry change."""

import json
from dataclasses import replace

from spendgate.core.budgets import ScopeKind
from spendgate.core.outcomes import Outcome, ReservationState
from spendgate.core.policy import KillSwitch, KillTarget
from spendgate.ledger import key_flow, sql
from spendgate.ledger.admin_flow import (
    budget_totals_flow,
    must_lock,
    scope_row,
)
from spendgate.ledger.admin_port import (
    AuditRow,
    BudgetView,
    CallCursor,
    CallFields,
    CallFilters,
    KillSwitchRow,
)
from spendgate.ledger.common import Ctx, audit, i, oi, s
from spendgate.ledger.key_types import KeyRecord
from spendgate.ledger.port import CallRow, ConflictError, HeldItem, ReservationRow, ScopeRow
from spendgate.ledger.sql import Flow, Row, Value, q

FAR_FUTURE = 2**62  # a bound that no epoch-ms value reaches, so filters need no NULL branches


def global_kill_flow() -> Flow[bool]:
    return bool((yield q(sql.KILL_GLOBAL)))


def active_kill_switches_flow() -> Flow[tuple[KillSwitch, ...]]:
    rows = yield q(sql.KILL_ACTIVE)
    return tuple(KillSwitch(KillTarget(s(r, "target_kind")), s(r, "target_ref")) for r in rows)


def pending_flow() -> Flow[int]:
    return i((yield q(sql.PENDING_COUNT))[0], "n")


def resolve_scope_flow(ref: str) -> Flow[ScopeRow | None]:
    rows = yield q(sql.SCOPE_BY_REF, ref=ref)
    return scope_row(rows[0]) if rows else None


def find_scope_flow(path: str) -> Flow[ScopeRow | None]:
    rows = yield q(sql.SCOPE_FIND, path=path)
    return scope_row(rows[0]) if rows else None


def list_scopes_flow(
    kind: ScopeKind | None, parent_id: str | None, after: str, limit: int
) -> Flow[tuple[ScopeRow, ...]]:
    rows = yield q(
        sql.SCOPES_LIST,
        kind=None if kind is None else kind.value,
        parent=parent_id,
        after=after,
        limit=limit,
    )
    return tuple(scope_row(r) for r in rows)


def _view_flow(budget_id: str, now: int) -> Flow[BudgetView]:
    budget = yield from must_lock(budget_id)
    totals = yield from budget_totals_flow(budget, now)
    extra = (yield q(sql.BUDGET_NOTE, id=budget_id))[0]
    note = extra["note"] if isinstance(extra["note"], str) else None
    return BudgetView(budget, totals, note, extra["disabled_at"] is not None)


def get_budget_flow(budget_id: str, now: int) -> Flow[BudgetView | None]:
    if not (yield q(sql.BUDGET_NOTE, id=budget_id)):
        return None
    return (yield from _view_flow(budget_id, now))


def list_budgets_flow(scope_id: str | None, now: int) -> Flow[tuple[BudgetView, ...]]:
    if scope_id is None:
        rows = yield q(sql.BUDGETS_ALL)
    else:
        rows = yield q(sql.BUDGETS_OF_SCOPE, scope=scope_id)
    views: list[BudgetView] = []
    for row in rows:
        views.append((yield from _view_flow(s(row, "id"), now)))
    return tuple(views)


def list_kill_switches_flow() -> Flow[tuple[KillSwitchRow, ...]]:
    rows = yield q(sql.KILL_LIST)
    return tuple(
        KillSwitchRow(
            s(r, "id"),
            KillSwitch(KillTarget(s(r, "target_kind")), s(r, "target_ref")),
            s(r, "reason"),
            s(r, "created_by"),
            i(r, "created_at"),
            oi(r, "cleared_at"),
        )
        for r in rows
    )


def set_key_expiry_flow(
    ctx: Ctx, key_id: str, expires_at: int | None, actor: str, now: int
) -> Flow[KeyRecord]:
    """Set or clear a key's expiry (the policy flow leaves it alone); one audit row.

    On a key a rotation replaced, or one already expired, the expiry may only move earlier:
    clearing or extending it would let an old secret spend again (SG-PT-10). The key row is
    locked first, so a rotation running at the same time is seen.
    """
    before = yield from key_flow.must_get(key_id)
    if _extends(before.expires_at, expires_at):
        if before.expires_at is not None and now >= before.expires_at:
            raise ConflictError(
                f"key {key_id} has expired; its expiry can only be moved earlier: "
                "issue a new key instead"
            )
        if (yield q(sql.KEY_SUCCESSOR, id=key_id)):
            raise ConflictError(
                f"key {key_id} was replaced by rotation; its expiry can only be moved earlier: "
                "use the key that replaced it"
            )
    yield q(sql.KEY_SET_EXPIRY, id=key_id, expires_at=expires_at)
    after = replace(before, expires_at=expires_at)
    yield from audit(
        ctx,
        now,
        actor,
        "key.update",
        ("key", key_id),
        {"expires_at": before.expires_at},
        {"expires_at": expires_at},
    )
    return after


def _extends(current: int | None, wanted: int | None) -> bool:
    """Whether `wanted` keeps a key valid past its `current` expiry (None: never expires)."""
    return current is not None and (wanted is None or wanted > current)


def list_audit_flow(
    since: int, until: int | None, actor: str | None, before: tuple[int, str] | None, limit: int
) -> Flow[tuple[AuditRow, ...]]:
    cursor_at, cursor_id = before if before is not None else (FAR_FUTURE, "")
    rows = yield q(
        sql.AUDIT_LIST,
        since=since,
        until=FAR_FUTURE if until is None else until,
        actor=actor,
        cursor_at=cursor_at,
        cursor_id=cursor_id,
        limit=limit,
    )
    return tuple(
        AuditRow(
            s(r, "id"),
            i(r, "at"),
            s(r, "actor_key_id"),
            s(r, "action"),
            s(r, "target_type"),
            s(r, "target_id"),
            None if r["before"] is None else s(r, "before"),
            None if r["after"] is None else s(r, "after"),
        )
        for r in rows
    )


def _call_fields(row: Row) -> CallFields:
    return dict(row)


def list_calls_flow(
    filters: CallFilters, before: CallCursor | None, limit: int
) -> Flow[tuple[CallFields, ...]]:
    cursor = before or CallCursor(FAR_FUTURE, "", 0)
    rows = yield q(
        sql.CALLS_LIST,
        scope_id=filters.scope_id,
        key_id=filters.key_id,
        session_id=filters.session_id,
        outcome=filters.outcome,
        since=filters.since,
        until=FAR_FUTURE if filters.until is None else filters.until,
        cf=cursor.finished_at,
        cr=cursor.request_id,
        ca=cursor.attempt,
        limit=limit,
    )
    return tuple(_call_fields(r) for r in rows)


def get_call_flow(request_id: str) -> Flow[CallFields | None]:
    rows = yield q(sql.CALL_LAST, id=request_id)
    return _call_fields(rows[0]) if rows else None


def audit_json(text: str | None) -> dict[str, Value] | None:
    """Decode a stored before/after document (always a JSON object or NULL)."""
    if text is None:
        return None
    loaded = json.loads(text)
    if not isinstance(loaded, dict):
        raise TypeError("an audit document must be a JSON object")
    return {str(k): v for k, v in loaded.items()}


def get_reservation_flow(request_id: str) -> Flow[ReservationRow | None]:
    rows = yield q(sql.RES_GET, id=request_id)
    if not rows:
        return None
    r = rows[0]
    items = yield q(sql.ITEMS_OF, id=request_id)
    outcome = r["outcome"]
    return ReservationRow(
        s(r, "id"),
        ReservationState(s(r, "state")),
        s(r, "owner_instance"),
        i(r, "lease_expires_at"),
        s(r, "key_id"),
        s(r, "scope_id"),
        r["session_id"] if isinstance(r["session_id"], str) else None,
        Outcome(outcome) if isinstance(outcome, str) else None,
        oi(r, "charged_nano"),
        tuple(HeldItem(s(x, "budget_id"), i(x, "period_start"), i(x, "held_nano")) for x in items),
        {s(x, "budget_id"): i(x, "overrun_nano") for x in items},
    )


def get_calls_flow(request_id: str) -> Flow[tuple[CallRow, ...]]:
    rows = yield q(sql.CALLS_OF, id=request_id)
    return tuple(
        CallRow(
            s(r, "request_id"),
            i(r, "attempt"),
            s(r, "provider"),
            s(r, "deployment"),
            Outcome(s(r, "outcome")),
            i(r, "charged_nano"),
            oi(r, "usage_cost_nano"),
            i(r, "hold_nano"),
            i(r, "overrun_nano"),
            s(r, "settled_by"),
            oi(r, "review_required_at"),
            i(r, "finished_at"),
            oi(r, "http_status"),
        )
        for r in rows
    )
