"""Admin routes that only read: usage, calls, audit and status."""

from datetime import UTC, datetime

from starlette.responses import Response

from spendgate.core.errors import ErrorCode
from spendgate.core.outcomes import Outcome
from spendgate.gateway.admin_codec import (
    audit_entry_json,
    call_json,
    kill_json,
    page,
    usage_json,
)
from spendgate.gateway.admin_http import (
    ApiError,
    Call,
    decode_cursor,
    encode_cursor,
    respond,
)
from spendgate.gateway.admin_scopes import need_scope
from spendgate.gateway.observability import compute_readiness
from spendgate.ledger.admin_port import BudgetView, CallCursor, CallFilters, KillSwitchRow
from spendgate.ledger.port import LedgerError, NotFoundError
from spendgate.ledger.usage import Basis, UsageFilters, UsageQuery

_PERCENT = 100


async def query_usage(call: Call) -> Response:
    by = tuple(d for d in (call.query("by") or "").split(",") if d)
    try:
        basis = Basis(call.query("basis") or "charged")
    except ValueError:
        raise ApiError(ErrorCode.INVALID_REQUEST, "basis is charged or usage.", "basis") from None
    cursor = decode_cursor(call.query("cursor"), (int,))
    offset, limit = (int(cursor[0]) if cursor else 0), call.limit()
    query = UsageQuery(
        by,
        call.time_query("since"),
        call.time_query("until"),
        UsageFilters(call.query("scope_id"), model=call.query("model"), tag=call.query("tag")),
        basis,
        limit,
        offset,
    )
    result = await call.ctx.ledger.usage(
        query, now=call.now, retention_days=call.ctx.settings.config.ledger.retention_days
    )
    nxt = encode_cursor(offset + limit) if result.has_more else None
    return respond(usage_json(result, nxt))


async def list_calls(call: Call) -> Response:
    outcome = call.query("outcome")
    if outcome is not None and outcome not in {o.value for o in Outcome}:
        raise ApiError(ErrorCode.INVALID_REQUEST, "Unknown outcome.", "outcome")

    async def ref(name: str) -> str | None:
        raw = call.query(name)
        return None if raw is None else (await need_scope(call, raw)).id

    filters = CallFilters(
        scope_id=await ref("scope_id"),
        key_id=call.query("key_id"),
        session_id=await ref("session_id"),
        outcome=outcome,
        since=call.time_query("since") or 0,
        until=call.time_query("until"),
    )
    raw_cursor = decode_cursor(call.query("cursor"), (int, str, int))
    before = None if raw_cursor is None else CallCursor(*raw_cursor)  # type: ignore[arg-type]
    limit = call.limit()
    rows = await call.ctx.ledger.list_calls(filters, before=before, limit=limit + 1)
    shown = rows[:limit]
    last = shown[-1] if shown else None
    more = len(rows) > limit and last is not None
    cursor = None
    if more and last is not None:
        cursor = encode_cursor(
            int(str(last["finished_at"])), str(last["request_id"]), int(str(last["attempt"]))
        )
    return respond(page([call_json(r) for r in shown], cursor))


async def get_call(call: Call) -> Response:
    row = await call.ctx.ledger.get_call(call.path("request_id"))
    if row is None:
        raise NotFoundError("call does not exist")
    return respond(call_json(row))


async def list_audit(call: Call) -> Response:
    raw_cursor = decode_cursor(call.query("cursor"), (int, str))
    before = None if raw_cursor is None else (int(raw_cursor[0]), str(raw_cursor[1]))
    limit = call.limit()
    rows = await call.ctx.ledger.list_audit(
        since=call.time_query("since") or 0,
        until=call.time_query("until"),
        actor=call.query("actor"),
        before=before,
        limit=limit + 1,
    )
    shown = rows[:limit]
    cursor = encode_cursor(shown[-1].at, shown[-1].id) if len(rows) > limit else None
    return respond(page([audit_entry_json(r) for r in shown], cursor))


# --- status ----------------------------------------------------------------------------------


def budget_alerts(views: tuple[BudgetView, ...]) -> list[dict[str, str]]:
    """Budgets awaiting review, exhausted, or past their soft threshold."""
    alerts: list[dict[str, str]] = []
    for v in views:
        t = v.totals
        used = t.spent_nano + t.held_nano
        if t.review_required_at is not None:
            kind = "overrun_review"
        elif used >= t.effective_cap_nano:
            kind = "exhausted"
        elif used * _PERCENT >= v.budget.soft_pct * t.effective_cap_nano:
            kind = "soft_threshold"
        else:
            continue
        alerts.append({"scope_id": v.budget.scope_id, "budget_id": v.budget.id, "kind": kind})
    return alerts


def _catalog_json(call: Call) -> dict[str, object]:
    today = datetime.fromtimestamp(call.now / 1000, UTC).date()
    entries = call.ctx.settings.catalog.entries()
    newest = max((e.effective_from for e in entries), default=None)
    stale = sorted(
        {e.id for e in entries if e.deprecation_date is not None and e.deprecation_date <= today}
    )
    out: dict[str, object] = {"entries": len(entries), "expired_or_deprecated": stale}
    if newest is not None:
        out["newest_effective_from"] = newest.isoformat()
    return out


async def readiness(call: Call) -> Response:
    """`/readyz` in detail: reasons and warnings, for an admin key (the data port hides them)."""
    checks = call.ctx.readiness
    if checks is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Readiness is not available here.")
    result = await compute_readiness(checks)
    return respond(result.body(detail=True), 200 if result.ready else 503)


async def status(call: Call) -> Response:
    ledger = call.ctx.ledger
    ledger_info: dict[str, object] = {
        "backend": call.ctx.settings.config.ledger.backend.kind,
        "healthy": True,
    }
    alerts: list[dict[str, str]] = []
    kills: tuple[KillSwitchRow, ...] = ()
    try:
        ledger_info["pending_holds"] = await ledger.pending_reservations()
        alerts = budget_alerts(await ledger.list_budgets(None, call.now))
        kills = await ledger.list_kill_switches()
    except LedgerError:
        ledger_info["healthy"] = False
    return respond(
        {
            "version": call.ctx.version,
            "uptime_seconds": max(0, (call.now - call.ctx.started_ms) // 1000),
            "ledger": ledger_info,
            "catalog": _catalog_json(call),
            "kill_switches": [kill_json(k) for k in kills],
            "budget_alerts": alerts,
        }
    )
