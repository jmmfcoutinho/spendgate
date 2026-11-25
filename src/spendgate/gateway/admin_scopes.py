"""Admin routes for scopes and virtual keys (OpenAPI tags `scopes` and `keys`)."""

from dataclasses import replace

from starlette.responses import Response

from spendgate.core.budgets import ScopeKind
from spendgate.core.errors import ErrorCode
from spendgate.core.policy import KeyPolicy
from spendgate.core.sessions import SessionDefaults
from spendgate.gateway.admin_codec import budget_json, key_json, page, scope_json
from spendgate.gateway.admin_http import (
    ApiError,
    Call,
    decode_cursor,
    encode_cursor,
    respond,
)
from spendgate.gateway.admin_models import (
    KeyCreateIn,
    KeyLimitsIn,
    KeyUpdateIn,
    RotateIn,
    ScopeCreateIn,
    ScopeUpdateIn,
    SessionDefaultsIn,
)
from spendgate.ledger.key_types import KeyKind, KeyRecord, KeySpec
from spendgate.ledger.port import NotFoundError, ScopeRow

_MS = 1_000


async def need_scope(call: Call, ref: str) -> ScopeRow:
    """A scope by id or path, or a 404."""
    found = await call.ctx.ledger.resolve_scope(ref)
    if found is None:
        raise NotFoundError(f"scope {ref} does not exist")
    return found


# --- scopes ----------------------------------------------------------------------------------


async def list_scopes(call: Call) -> Response:
    kind_raw = call.query("kind")
    try:
        kind = None if kind_raw is None else ScopeKind(kind_raw)
    except ValueError:
        raise ApiError(ErrorCode.INVALID_REQUEST, "Unknown scope kind.", "kind") from None
    parent_ref = call.query("parent")
    parent = None if parent_ref is None else (await need_scope(call, parent_ref)).id
    cursor = decode_cursor(call.query("cursor"), (str,))
    limit = call.limit()
    rows = await call.ctx.ledger.list_scopes(
        kind=kind, parent_id=parent, after=str(cursor[0]) if cursor else "", limit=limit + 1
    )
    more = len(rows) > limit
    shown = rows[:limit]
    return respond(
        page([scope_json(r) for r in shown], encode_cursor(shown[-1].id) if more else None)
    )


async def create_scope(call: Call) -> Response:
    body = await call.body(ScopeCreateIn)
    *parents, last = body.path.split("/")
    kind_name, _, name = last.partition(":")
    try:
        kind = ScopeKind(kind_name)
    except ValueError:
        raise ApiError(ErrorCode.INVALID_REQUEST, "Unknown scope kind.", "path") from None
    if kind is ScopeKind.SESSION:
        raise ApiError(
            ErrorCode.INVALID_REQUEST, "Session scopes are created by the gateway.", "path"
        )
    parent_id = None
    if parents:
        found = await call.ctx.ledger.resolve_scope("/".join(parents))
        if found is None:
            raise ApiError(ErrorCode.NOT_FOUND, "The parent scope does not exist.", "path")
        parent_id = found.id
    row = await call.ctx.ledger.create_scope(
        kind, name, parent_id, now=call.now, actor=call.actor, display_name=body.display_name
    )
    return respond(scope_json(row), 201)


async def get_scope(call: Call) -> Response:
    row = await need_scope(call, call.path("scope_id"))
    views = await call.ctx.ledger.list_budgets(row.id, call.now)
    detail = scope_json(row) | {"budgets": [budget_json(v, call.ctx.currency) for v in views]}
    return respond(detail)


async def update_scope(call: Call) -> Response:
    row = await need_scope(call, call.path("scope_id"))
    body = await call.body(ScopeUpdateIn)
    if "display_name" not in body.model_fields_set:
        return respond(scope_json(row))
    changed = await call.ctx.ledger.update_scope(
        row.id, display_name=body.display_name, actor=call.actor, now=call.now
    )
    return respond(scope_json(changed))


# --- keys ------------------------------------------------------------------------------------


def _limits(body: KeyLimitsIn | None) -> dict[str, int | None]:
    given = body or KeyLimitsIn()
    return {n: getattr(given, n) for n in KeyLimitsIn.model_fields}


def _sessions(body: SessionDefaultsIn | None, required: bool) -> SessionDefaults | None:
    """`session_required` is stored with the session defaults, so it cannot stand without them."""
    if body is None:
        if required:
            raise ApiError(
                ErrorCode.INVALID_REQUEST,
                "session_required needs session_defaults.",
                "session_required",
            )
        return None
    return SessionDefaults(**body.model_dump(), required=required)


async def create_key(call: Call) -> Response:
    body = await call.body(KeyCreateIn)
    scope = await need_scope(call, body.scope_id)
    limits = body.limits or KeyLimitsIn()
    spec = KeySpec(
        scope.id,
        body.name or "key",
        KeyKind.API,
        None if body.allowed_models is None else tuple(body.allowed_models),
        limits.max_output_tokens,
        limits.per_call_ceiling_nano,
        limits.rpm,
        limits.tpm,
        limits.max_concurrent,
        _sessions(body.session_defaults, body.session_required),
        expires_at=body.expires_at,
    )
    made = await call.ctx.keys.create_key(spec, actor=call.actor, now=call.now)
    return respond(key_json(made.record) | {"secret": made.secret}, 201)


async def need_api_key(call: Call) -> KeyRecord:
    """An api key by id; admin keys are managed on the host, not through this API."""
    found = await call.ctx.ledger.get_key(call.path("key_id"))
    if found is None or found.kind is not KeyKind.API:
        raise NotFoundError("key does not exist")
    return found


async def list_keys(call: Call) -> Response:
    scope_ref = call.query("scope_id")
    scope_id = None if scope_ref is None else (await need_scope(call, scope_ref)).id
    include_revoked = call.query("include_revoked") == "true"
    rows = [
        r
        for r in await call.ctx.ledger.list_keys(scope_id)
        if include_revoked or r.revoked_at is None
    ]
    cursor = decode_cursor(call.query("cursor"), (int,))
    start = int(cursor[0]) if cursor else 0
    limit = call.limit()
    shown = rows[start : start + limit]
    more = start + limit < len(rows)
    return respond(
        page([key_json(r) for r in shown], encode_cursor(start + limit) if more else None)
    )


async def get_key(call: Call) -> Response:
    return respond(key_json(await need_api_key(call)))


def _patched_sessions(current: SessionDefaults | None, body: KeyUpdateIn) -> SessionDefaults | None:
    """Absent fields keep their stored value: new limits keep `required`, and the reverse."""
    given = body.model_fields_set
    if "session_required" in given:
        required = body.session_required
    else:
        required = current is not None and current.required
    if "session_defaults" not in given:
        if current is None:
            return _sessions(None, required)
        return replace(current, required=required)
    return _sessions(body.session_defaults, required)


def _patched_policy(current: KeyPolicy, body: KeyUpdateIn) -> KeyPolicy:
    changes: dict[str, object] = {}
    given = body.model_fields_set
    if "allowed_models" in given:
        changes["allowed_models"] = (
            None if body.allowed_models is None else tuple(body.allowed_models)
        )
    if given & {"session_defaults", "session_required"}:
        changes["session_defaults"] = _patched_sessions(current.session_defaults, body)
    if "limits" in given:  # the whole limits object is replaced, as a PATCH of that field
        changes.update(_limits(body.limits))
    return replace(current, **changes)  # type: ignore[arg-type]


async def update_key(call: Call) -> Response:
    current = await need_api_key(call)
    body = await call.body(KeyUpdateIn)
    given = body.model_fields_set
    record = current
    if given & {"name", "allowed_models", "limits", "session_defaults", "session_required"}:
        record = await call.ctx.keys.set_key_policy(
            _patched_policy(current.policy, body),
            name=body.name,
            actor=call.actor,
            now=call.now,
        )
    if "expires_at" in given:
        record = await call.ctx.ledger.set_key_expiry(
            current.id, body.expires_at, actor=call.actor, now=call.now
        )
        call.ctx.keys.invalidate(current.id)
    return respond(key_json(record))


async def revoke_key(call: Call) -> Response:
    current = await need_api_key(call)
    revoked = await call.ctx.keys.revoke_key(current.id, actor=call.actor, now=call.now)
    return respond(key_json(revoked))


async def rotate_key(call: Call) -> Response:
    current = await need_api_key(call)
    body = await call.body(RotateIn, optional=True)
    made = await call.ctx.keys.rotate_key(
        current.id, grace_ms=body.grace_seconds * _MS, actor=call.actor, now=call.now
    )
    return respond(key_json(made.record) | {"secret": made.secret}, 201)
