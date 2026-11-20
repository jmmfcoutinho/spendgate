"""Key transactions as sans-IO flows: create, rotate, revoke, set policy, lookup.

Every change writes its `admin_audit` row in the same transaction. Audit rows hold settings
only: never a secret and never a hash.
"""

import json
from dataclasses import replace

from spendgate.core.policy import KeyPolicy
from spendgate.core.sessions import SessionDefaults
from spendgate.ledger import keys, sql
from spendgate.ledger.common import Ctx, audit, oi, s
from spendgate.ledger.key_types import (
    AdminScope,
    KeyKind,
    KeyRecord,
    KeySpec,
    NewKey,
    StoredKey,
)
from spendgate.ledger.port import ConflictError, NotFoundError
from spendgate.ledger.sql import Flow, Row, Value, q

_ATTEMPTS = 5  # prefix collisions: 48 random bits, so one retry is already astronomically rare
_LIMIT_FIELDS = ("rpm", "tpm", "max_concurrent", "max_output_tokens", "per_call_ceiling_nano")
_SESSION_FIELDS = tuple(SessionDefaults.__slots__)


# --- row <-> record --------------------------------------------------------------------------


def _json_object(text: Value) -> dict[str, object]:
    if text is None:
        return {}
    loaded = json.loads(str(text))
    if not isinstance(loaded, dict):
        raise TypeError("a key JSON column must hold an object")
    return {str(k): v for k, v in loaded.items()}


def _opt_int(obj: dict[str, object], name: str) -> int | None:
    value = obj.get(name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} is not an integer")
    return value


def _json_list(text: Value) -> list[str] | None:
    if text is None:
        return None
    loaded = json.loads(str(text))
    if not isinstance(loaded, list) or not all(isinstance(x, str) for x in loaded):
        raise TypeError("a key JSON list column must hold strings")
    return [str(x) for x in loaded]


def record_from_row(row: Row) -> KeyRecord:
    limits = _json_object(row["limits"])
    models = _json_list(row["allowed_models"])
    sessions = None
    if row["session_defaults"] is not None:
        raw = _json_object(row["session_defaults"])
        sessions = SessionDefaults(**{f: _opt_int(raw, f) for f in _SESSION_FIELDS})
    policy = KeyPolicy(
        s(row, "id"),
        s(row, "scope_id"),
        None if models is None else tuple(models),
        _opt_int(limits, "max_output_tokens"),
        _opt_int(limits, "per_call_ceiling_nano"),
        _opt_int(limits, "rpm"),
        _opt_int(limits, "tpm"),
        _opt_int(limits, "max_concurrent"),
        sessions,
    )
    perms = frozenset(AdminScope(p) for p in _json_list(row["admin_perms"]) or [])
    return KeyRecord(
        s(row, "id"),
        s(row, "prefix"),
        KeyKind(s(row, "kind")),
        s(row, "name"),
        _created(row),
        policy,
        perms,
        oi(row, "expires_at"),
        oi(row, "revoked_at"),
        None if row["replaces_key_id"] is None else s(row, "replaces_key_id"),
    )


def _created(row: Row) -> int:
    value = oi(row, "created_at")
    if value is None:
        raise TypeError("created_at is null")
    return value


def _policy_columns(policy: KeyPolicy) -> dict[str, Value]:
    limits = {f: getattr(policy, f) for f in _LIMIT_FIELDS if getattr(policy, f) is not None}
    sessions = policy.session_defaults
    return {
        "allowed_models": None
        if policy.allowed_models is None
        else json.dumps(policy.allowed_models),
        "limits": json.dumps(limits, sort_keys=True) if limits else None,
        "session_defaults": None
        if sessions is None
        else json.dumps(
            {f: getattr(sessions, f) for f in _SESSION_FIELDS if getattr(sessions, f) is not None},
            sort_keys=True,
        ),
    }


def _audit_view(rec: KeyRecord) -> dict[str, Value]:
    """Flat settings for `admin_audit.before/after`; no secret, no hash."""
    view: dict[str, Value] = {
        "prefix": rec.prefix,
        "kind": rec.kind.value,
        "name": rec.name,
        "scope_id": rec.scope_id,
        "expires_at": rec.expires_at,
        "revoked_at": rec.revoked_at,
        "admin_scopes": ",".join(sorted(p.value for p in rec.admin_scopes)),
    }
    view.update(_policy_columns(rec.policy))
    return view


def spec_of(rec: KeyRecord) -> KeySpec:
    """A new key with the same settings (rotation)."""
    p = rec.policy
    return KeySpec(
        rec.scope_id,
        rec.name,
        rec.kind,
        p.allowed_models,
        p.max_output_tokens,
        p.per_call_ceiling_nano,
        p.rpm,
        p.tpm,
        p.max_concurrent,
        p.session_defaults,
        rec.admin_scopes,
        rec.expires_at,
    )


# --- flows -----------------------------------------------------------------------------------


def find_flow(prefix: str) -> Flow[StoredKey | None]:
    rows = yield q(sql.KEY_BY_PREFIX, prefix=prefix)
    if not rows:
        return None
    return StoredKey(record_from_row(rows[0]), s(rows[0], "secret_hash"))


def get_flow(key_id: str) -> Flow[KeyRecord | None]:
    rows = yield q(sql.KEY_GET, id=key_id)
    return record_from_row(rows[0]) if rows else None


def list_flow(kind: KeyKind, scope_id: str | None) -> Flow[tuple[KeyRecord, ...]]:
    if scope_id is None:
        rows = yield q(sql.KEY_LIST, kind=kind.value)
    else:
        rows = yield q(sql.KEY_LIST_SCOPE, kind=kind.value, scope_id=scope_id)
    return tuple(record_from_row(r) for r in rows)


def _insert(ctx: Ctx, spec: KeySpec, pepper: bytes, now: int, replaces: str | None) -> Flow[NewKey]:
    """Insert one key under a fresh prefix (retried on the rare collision); returns the secret."""
    if not (yield q(sql.SCOPE_GET, id=spec.scope_id)):
        raise NotFoundError(f"scope {spec.scope_id} does not exist")
    for _ in range(_ATTEMPTS):
        prefix, secret = keys.new_prefix(), keys.new_secret()
        if (yield q(sql.KEY_BY_PREFIX, prefix=prefix)):
            continue
        key_id = ctx.new_id()
        _models_not_empty(spec.allowed_models)
        policy = KeyPolicy(
            key_id,
            spec.scope_id,
            spec.allowed_models,
            spec.max_output_tokens,
            spec.per_call_ceiling_nano,
            spec.rpm,
            spec.tpm,
            spec.max_concurrent,
            spec.session_defaults,
        )
        perms = sorted(p.value for p in spec.admin_scopes)
        yield q(
            sql.KEY_FULL_INSERT,
            id=key_id,
            prefix=prefix,
            secret_hash=keys.hash_secret(secret, pepper),
            kind=spec.kind.value,
            scope_id=spec.scope_id,
            name=spec.name,
            admin_perms=json.dumps(perms) if perms else None,
            now=now,
            expires_at=spec.expires_at,
            replaces_key_id=replaces,
            **_policy_columns(policy),
        )
        record = KeyRecord(
            key_id,
            prefix,
            spec.kind,
            spec.name,
            now,
            policy,
            spec.admin_scopes,
            spec.expires_at,
            None,
            replaces,
        )
        return NewKey(record, keys.format_key(spec.kind, prefix, secret))
    raise ConflictError("could not find a free key prefix")


def create_key_flow(ctx: Ctx, spec: KeySpec, pepper: bytes, actor: str, now: int) -> Flow[NewKey]:
    made = yield from _insert(ctx, spec, pepper, now, None)
    yield from audit(
        ctx, now, actor, "key.create", ("key", made.record.id), None, _audit_view(made.record)
    )
    return made


def must_get(key_id: str) -> Flow[KeyRecord]:
    rows = yield q(sql.KEY_LOCK, id=key_id)
    if not rows:
        raise NotFoundError(f"key {key_id} does not exist")
    return record_from_row(rows[0])


def revoke_flow(ctx: Ctx, key_id: str, actor: str, now: int) -> Flow[KeyRecord]:
    """Revoking twice keeps the first `revoked_at` and writes no second audit row."""
    before = yield from must_get(key_id)
    if before.revoked_at is not None:
        return before
    yield q(sql.KEY_REVOKE, id=key_id, now=now)
    after = replace(before, revoked_at=now)
    yield from audit(
        ctx, now, actor, "key.revoke", ("key", key_id), _audit_view(before), _audit_view(after)
    )
    return after


def rotate_flow(  # noqa: PLR0913, PLR0917 - mirrors the admin rotate body plus pepper and actor
    ctx: Ctx, key_id: str, grace_ms: int, pepper: bytes, actor: str, now: int
) -> Flow[NewKey]:
    """New key with the same settings; the old one expires after the grace period."""
    if grace_ms < 0:
        raise ValueError("grace_ms must not be negative")
    old = yield from must_get(key_id)
    if old.revoked_at is not None or (old.expires_at is not None and now >= old.expires_at):
        raise ConflictError(f"key {key_id} is revoked or expired and cannot be rotated")
    made = yield from _insert(ctx, spec_of(old), pepper, now, key_id)
    until = now + grace_ms if old.expires_at is None else min(old.expires_at, now + grace_ms)
    yield q(sql.KEY_SET_EXPIRY, id=key_id, expires_at=until)
    yield from audit(
        ctx, now, actor, "key.create", ("key", made.record.id), None, _audit_view(made.record)
    )
    yield from audit(
        ctx,
        now,
        actor,
        "key.rotate",
        ("key", key_id),
        _audit_view(old),
        _audit_view(replace(old, expires_at=until)),
    )
    return made


def _models_not_empty(models: tuple[str, ...] | None) -> None:
    """`()` must not silently become "every model" (SR-09); None is the way to say that."""
    if models is not None and not models:
        raise ValueError("allowed_models must not be empty; leave it out to allow every model")


def set_policy_flow(
    ctx: Ctx, policy: KeyPolicy, name: str | None, actor: str, now: int
) -> Flow[KeyRecord]:
    """Replace allowed models, limits and session defaults (the scope never changes)."""
    before = yield from must_get(policy.id)
    if policy.scope_id != before.scope_id:
        raise ValueError("a key cannot move to another scope; rotate into a new key instead")
    if before.kind is KeyKind.ADMIN:
        raise ValueError("an admin key has no policy; it has scopes")
    _models_not_empty(policy.allowed_models)
    KeySpec(
        before.scope_id,
        name or before.name,
        KeyKind.API,
        policy.allowed_models,
        policy.max_output_tokens,
        policy.per_call_ceiling_nano,
        policy.rpm,
        policy.tpm,
        policy.max_concurrent,
        policy.session_defaults,
    )
    after = replace(
        before,
        name=name or before.name,
        policy=policy,
    )
    yield q(sql.KEY_SET_POLICY, id=policy.id, name=after.name, **_policy_columns(after.policy))
    yield from audit(
        ctx, now, actor, "key.update", ("key", policy.id), _audit_view(before), _audit_view(after)
    )
    return after
