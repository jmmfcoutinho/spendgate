"""Ledger objects as the JSON of the admin API (OpenAPI component schemas)."""

import json
from collections.abc import Mapping

from spendgate.core.policy import KillTarget
from spendgate.core.sessions import LIMIT_FIELDS as _SESSION_FIELDS
from spendgate.gateway.admin_http import iso, iso_or_none, money_json
from spendgate.ledger.admin_port import AuditRow, BudgetView, CallFields, KillSwitchRow
from spendgate.ledger.admin_reads import audit_json
from spendgate.ledger.key_types import KeyRecord
from spendgate.ledger.port import ScopeRow
from spendgate.ledger.usage import UsageResult, UsageRow

type Json = dict[str, object]
_LIMIT_FIELDS = ("rpm", "tpm", "max_concurrent", "max_output_tokens", "per_call_ceiling_nano")
_CALL_TIMES = ("started_at", "finished_at", "review_required_at")


def scope_json(row: ScopeRow) -> Json:
    out: Json = {
        "id": row.id,
        "path": row.path,
        "kind": row.kind.value,
        "parent_id": row.parent_id,
        "created_at": iso(row.created_at),
    }
    if row.display_name is not None:
        out["display_name"] = row.display_name
    return out


def _present(obj: object, names: tuple[str, ...]) -> Json:
    found = {n: getattr(obj, n) for n in names}
    return {k: v for k, v in found.items() if v is not None}


def key_json(rec: KeyRecord) -> Json:
    policy = rec.policy
    out: Json = {
        "id": rec.id,
        "prefix": rec.prefix,
        "name": rec.name,
        "scope_id": rec.scope_id,
        "limits": _present(policy, _LIMIT_FIELDS),
        "created_at": iso(rec.created_at),
        "expires_at": iso_or_none(rec.expires_at),
        "last_used_at": None,  # not tracked: a write per call would cost the hot path
        "revoked": rec.revoked_at is not None,
        "session_required": policy.session_defaults is not None
        and policy.session_defaults.required,
    }
    if policy.allowed_models is not None:
        out["allowed_models"] = list(policy.allowed_models)
    if policy.session_defaults is not None:
        sessions = _present(policy.session_defaults, _SESSION_FIELDS)
        # Absent in a request means 60, so an unbounded key shows `null`: a GET-then-PATCH round
        # trip must not turn an explicit opt-out into the default.
        sessions.setdefault("max_new_sessions_per_hour", None)
        out["session_defaults"] = sessions
    return out


def budget_json(view: BudgetView, currency: str) -> Json:
    b, t = view.budget, view.totals
    out: Json = {
        "id": b.id,
        "scope_id": b.scope_id,
        "period": b.period.value,
        "cap": money_json(b.cap_nano, currency),
        "soft_pct": b.soft_pct,
        "model_filter": b.model_filter,
        "strict_overrun": b.strict_overrun,
        "period_start": iso(t.period_start) if b.period.value != "none" else None,
        "effective_cap": money_json(t.effective_cap_nano, currency),
        "spent": money_json(t.spent_nano, currency),
        "held": money_json(t.held_nano, currency),
        "remaining": money_json(t.effective_cap_nano - t.spent_nano - t.held_nano, currency),
        "overrun": money_json(t.overrun_nano, currency),
        "excess": money_json(t.excess_nano, currency),
        "review_required_at": iso_or_none(t.review_required_at),
    }
    if view.note is not None:
        out["note"] = view.note
    return out


def kill_json(row: KillSwitchRow) -> Json:
    out: Json = {
        "id": row.id,
        "target_kind": row.switch.target_kind.value,
        "reason": row.reason,
        "created_by": row.created_by,
        "created_at": iso(row.created_at),
        "cleared_at": iso_or_none(row.cleared_at),
    }
    if row.switch.target_kind is not KillTarget.GLOBAL:
        out["target_ref"] = row.switch.target_ref
    return out


def audit_entry_json(row: AuditRow) -> Json:
    return {
        "id": row.id,
        "at": iso(row.at),
        "actor": row.actor,
        "action": row.action,
        "target": f"{row.target_type}:{row.target_id}",
        "before": audit_json(row.before),
        "after": audit_json(row.after),
    }


def call_json(row: CallFields) -> Json:
    out: Json = dict(row)
    for name in _CALL_TIMES:
        value = row[name]
        out[name] = iso(value) if isinstance(value, int) else None
    raw_labels = row["labels"]
    out["labels"] = json.loads(raw_labels) if isinstance(raw_labels, str) else {}
    out["streamed"] = bool(row["streamed"])
    return out


def _usage_row_json(row: UsageRow, currency: str) -> Json:
    return {
        "group": dict(row.group),
        "calls": row.calls,
        "tokens_in": row.tokens_in,
        "tokens_out": row.tokens_out,
        "tokens_cached": row.tokens_cached,
        "tokens_cache_write": row.tokens_cache_write,
        "audio_ms": row.audio_ms,
        "characters": row.characters,
        "cost": money_json(row.cost_nano, currency),
    }


def usage_json(result: UsageResult, next_cursor: str | None) -> Json:
    rows = [_usage_row_json(r, result.currency) for r in result.rows]
    return {
        "currency": result.currency,
        "basis": result.basis.value,
        "rows": rows,
        "total": money_json(result.total.cost_nano, result.currency),
        "total_calls": result.total.calls,
        "tags_overlap": result.tags_overlap,
        "next_cursor": next_cursor,
    }


def page(items: list[Json], next_cursor: str | None) -> Mapping[str, object]:
    return {"data": items, "next_cursor": next_cursor}
