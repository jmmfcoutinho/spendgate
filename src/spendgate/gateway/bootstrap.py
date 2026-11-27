"""Apply the config `bootstrap` section: create-only and idempotent.

A restart never changes an existing row. Scopes are found by path (missing ancestors are
created), budgets by the id `bootstrap-<config id>`, keys by name within their scope. A key's
secret exists only in the report of the run that created it. Admin keys need a scope row for
the foreign key; they live under `org:spendgate-admin`, which carries no budget and no traffic.

`serve` applies scopes and budgets but not keys (SR-08, C-17): a secret printed by a server is a
secret in the container log. Keys are created by `migrate --bootstrap`, which hands each secret
over once, to a terminal or a 0600 file; `keys_to_create` lets it refuse first when it has neither
(ADR-0032).
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from spendgate.config import Settings
from spendgate.config.schema import BootstrapBudget, BootstrapKey
from spendgate.core.budgets import Period, ScopeKind
from spendgate.core.sessions import SessionDefaults
from spendgate.ledger.admin_port import AdminLedger
from spendgate.ledger.key_types import AdminScope, KeyKind, KeyRecord, KeySpec
from spendgate.ledger.keys import KeyService
from spendgate.ledger.port import ScopeRow

ACTOR = "bootstrap"
ADMIN_SCOPE_PATH = "org:spendgate-admin"


@dataclass(frozen=True, slots=True)
class CreatedKey:
    name: str
    kind: str
    scope: str
    secret: str = field(repr=False)


type KeySink = Callable[[CreatedKey], None]


@dataclass(slots=True)
class BootstrapReport:
    scopes: list[str] = field(default_factory=list)
    budgets: list[str] = field(default_factory=list)
    keys: list[CreatedKey] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    on_key: KeySink | None = field(default=None, repr=False, compare=False)

    def add_key(self, key: CreatedKey) -> None:
        self.keys.append(key)
        if self.on_key is not None:  # at once: a later failure must not lose an issued secret
            self.on_key(key)


async def _ensure_scope(
    ledger: AdminLedger, path: str, now: int, report: BootstrapReport
) -> ScopeRow:
    """The scope at `path`, creating it and any missing ancestor (parents first)."""
    segments = path.split("/")
    parent: ScopeRow | None = None
    for depth in range(1, len(segments) + 1):
        current = "/".join(segments[:depth])
        found = await ledger.find_scope(current)
        if found is None:
            kind, _, name = segments[depth - 1].partition(":")
            found = await ledger.create_scope(
                ScopeKind(kind), name, parent.id if parent else None, now=now, actor=ACTOR
            )
            report.scopes.append(current)
        parent = found
    if parent is None:  # unreachable: a path has at least one segment
        raise ValueError("empty scope path")
    return parent


async def _budget(
    ledger: AdminLedger, settings: Settings, b: BootstrapBudget, now: int, report: BootstrapReport
) -> None:
    budget_id = f"bootstrap-{b.id}"
    if await ledger.get_budget(budget_id, now) is not None:
        return  # exists (possibly soft-deleted): create-only means never resurrected or changed
    scope = await _ensure_scope(ledger, b.scope, now, report)
    strict = b.strict_overrun
    if strict is None:
        strict = settings.config.defaults.strict_overrun
    await ledger.create_budget(
        scope.id,
        Period(b.period),
        b.cap.nano,
        now=now,
        soft_pct=b.soft_pct,
        model_filter=b.model_filter,
        strict_overrun=strict,
        note=f"bootstrap:{b.id}",
        actor=ACTOR,
        budget_id=budget_id,
    )
    report.budgets.append(b.id)


def _session_defaults(k: BootstrapKey) -> SessionDefaults | None:
    s = k.session_defaults
    if s is None:
        return None
    budget = None if s.budget is None else s.budget.nano
    return SessionDefaults(
        budget,
        s.max_steps,
        s.max_seconds,
        s.idle_seconds,
        s.max_new_sessions_per_hour,
        required=k.session_required,
    )


def _check(k: BootstrapKey) -> None:
    """Refused before any row is written: a later key's error must not strand an earlier secret."""
    if k.models is not None and not k.models:
        raise ValueError(
            f"bootstrap key {k.name}: models must not be empty; leave it out to allow every model"
        )


def _spec(k: BootstrapKey, scope: ScopeRow) -> KeySpec:
    _check(k)
    if k.admin_scopes is not None:
        return KeySpec(
            scope.id,
            k.name,
            KeyKind.ADMIN,
            admin_scopes=frozenset(AdminScope(s) for s in k.admin_scopes),
        )
    models = None if k.models is None else tuple(k.models)
    return KeySpec(
        scope.id,
        k.name,
        KeyKind.API,
        models,
        k.max_output_tokens,
        None,
        k.limits.rpm,
        k.limits.tpm,
        k.limits.concurrency,
        _session_defaults(k),
    )


def _kind(k: BootstrapKey) -> KeyKind:
    return KeyKind.ADMIN if k.admin_scopes is not None else KeyKind.API


def _path(k: BootstrapKey) -> str:
    return k.scope if k.scope is not None else ADMIN_SCOPE_PATH


async def _key(
    ledger: AdminLedger, keys: KeyService, k: BootstrapKey, now: int, report: BootstrapReport
) -> None:
    path, kind = _path(k), _kind(k)
    scope = await _ensure_scope(ledger, path, now, report)
    existing = [r for r in await ledger.list_keys(scope.id, kind=kind) if r.name == k.name]
    if existing:  # a revoked key is not recreated either
        _warn_session_drift(k, path, existing, now, report)
        return
    made = await keys.create_key(_spec(k, scope), actor=ACTOR, now=now)
    report.add_key(CreatedKey(k.name, kind.value, path, made.secret))


def _warn_session_drift(
    k: BootstrapKey, path: str, existing: list[KeyRecord], now: int, report: BootstrapReport
) -> None:
    """Create-only would keep an old key's session settings silently; `session_required` is a
    security setting, so a difference from the config is reported, never applied. Revoked and
    expired keys (the old key after a rotation) take no calls, so they are not reported."""
    if k.admin_scopes is not None:
        return
    wanted = _session_defaults(k)
    for rec in existing:
        live = rec.revoked_at is None and (rec.expires_at is None or now < rec.expires_at)
        if live and rec.policy.session_defaults != wanted:
            report.warnings.append(
                f"key {k.name} ({path}) exists with other session_required or session_defaults "
                f"than the config; bootstrap applies them only when it creates a key: change the "
                f"existing key with PATCH /admin/v1/keys/{rec.id}"
            )


async def keys_to_create(settings: Settings, ledger: AdminLedger) -> list[str]:
    """`name (scope)` of each configured key that `apply_bootstrap` would create. Reads only."""
    missing = []
    for k in settings.config.bootstrap.keys:
        scope = await ledger.find_scope(_path(k))
        rows = () if scope is None else await ledger.list_keys(scope.id, kind=_kind(k))
        if all(r.name != k.name for r in rows):  # revoked keys count: they are not recreated
            missing.append(f"{k.name} ({_path(k)})")
    return missing


async def apply_bootstrap(  # noqa: PLR0913 - the ports, then keyword-only options
    settings: Settings,
    ledger: AdminLedger,
    keys: KeyService,
    now: int,
    *,
    create_keys: bool = True,
    on_key: KeySink | None = None,
) -> BootstrapReport:
    """`on_key` gets each new key as soon as it exists, before the next one is made."""
    report = BootstrapReport(on_key=on_key)
    cfg = settings.config.bootstrap
    wanted = cfg.keys if create_keys else []
    for key in wanted:
        _check(key)
    for path in cfg.scopes:
        await _ensure_scope(ledger, path, now, report)
    for budget in cfg.budgets:
        await _budget(ledger, settings, budget, now, report)
    for key in wanted:
        await _key(ledger, keys, key, now, report)
    return report
