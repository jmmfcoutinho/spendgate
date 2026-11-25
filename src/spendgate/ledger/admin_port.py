"""The admin port: what the admin API, the CLI and set-up use beyond `MoneyLedger`, plus the plain
data its reads return.

`AdminLedger` extends the money port: every backend implements it through `LedgerEngine`, so the
gateway's admin layer and the CLI's `--local` mode share one set of functions. Code on the
request path takes `MoneyLedger` only.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from spendgate.core.budgets import Budget, Period, ScopeKind
from spendgate.core.policy import KeyPolicy, KillSwitch
from spendgate.ledger.key_types import AdminScope, KeyKind, KeyRecord, KeySpec, NewKey, StoredKey
from spendgate.ledger.port import BudgetTotals, CallRow, MoneyLedger, ReservationRow, ScopeRow
from spendgate.ledger.usage import RetentionResult, UsageQuery, UsageResult

# One `calls` row with the column names as keys (the admin `Call` schema mirrors the table).
type CallFields = Mapping[str, int | str | None]


@dataclass(frozen=True, slots=True)
class BudgetView:
    """A budget with its current period numbers; `disabled` is a soft delete."""

    budget: Budget
    totals: BudgetTotals
    note: str | None
    disabled: bool


@dataclass(frozen=True, slots=True)
class KillSwitchRow:
    id: str
    switch: KillSwitch
    reason: str
    created_by: str
    created_at: int
    cleared_at: int | None


@dataclass(frozen=True, slots=True)
class AuditRow:
    id: str
    at: int
    actor: str
    action: str
    target_type: str
    target_id: str
    before: str | None  # JSON text as stored
    after: str | None


@dataclass(frozen=True, slots=True)
class CallFilters:
    """Exact-match filters for `list_calls`; `since <= finished_at < until`."""

    scope_id: str | None = None
    key_id: str | None = None
    session_id: str | None = None
    outcome: str | None = None
    since: int = 0
    until: int | None = None


@dataclass(frozen=True, slots=True)
class CallCursor:
    """Where the previous page ended (the list is newest first)."""

    finished_at: int
    request_id: str
    attempt: int


class AdminLedger(MoneyLedger, Protocol):
    """The admin writes and reads on top of the money port. Names follow the admin operation ids."""

    # Admin side: the smallest set that creates and changes the state the scenarios run on.
    async def create_scope(  # noqa: PLR0913 - mirrors the admin ScopeCreate body
        self,
        kind: ScopeKind,
        name: str,
        parent_id: str | None,
        *,
        now: int,
        actor: str = "system",
        display_name: str | None = None,
    ) -> ScopeRow: ...

    async def create_budget(  # noqa: PLR0913 - mirrors the admin BudgetCreate body
        self,
        scope_id: str,
        period: Period,
        cap_nano: int,
        *,
        now: int,
        soft_pct: int = 80,
        model_filter: str | None = None,
        strict_overrun: bool = True,
        note: str | None = None,
        actor: str = "system",
        budget_id: str | None = None,
    ) -> Budget: ...

    async def update_budget(  # noqa: PLR0913 - mirrors the admin BudgetUpdate body
        self,
        budget_id: str,
        *,
        actor: str,
        now: int,
        cap_nano: int | None = None,
        soft_pct: int | None = None,
        strict_overrun: bool | None = None,
        note: str | None = None,
    ) -> Budget: ...

    async def create_grant(  # noqa: PLR0913 - mirrors the admin GrantCreate body
        self,
        budget_id: str,
        *,
        amount_nano: int,
        valid_from: int,
        valid_until: int,
        actor: str,
        reason: str,
        now: int,
    ) -> str: ...

    async def ack_overrun(self, budget_id: str, *, actor: str, reason: str, now: int) -> None: ...

    async def engage_kill_switch(
        self, switch: KillSwitch, *, actor: str, reason: str, now: int
    ) -> str: ...

    async def lift_kill_switch(self, switch_id: str, *, actor: str, now: int) -> None: ...

    async def delete_budget(self, budget_id: str, *, actor: str, now: int) -> None: ...

    async def disable_scope(self, scope_id: str, *, actor: str, now: int) -> None: ...

    async def add_key_record(self, key_id: str, scope_id: str, *, now: int) -> None:
        """Thin primitive that satisfies `reservations.key_id`; key flows handle real keys."""
        ...

    # Keys. `pepper` is passed in by the caller and never stored.
    async def create_key(self, spec: KeySpec, *, pepper: bytes, actor: str, now: int) -> NewKey: ...

    async def create_admin_key(  # noqa: PLR0913 - an admin key's whole definition
        self,
        scope_id: str,
        name: str,
        admin_scopes: frozenset[AdminScope],
        *,
        pepper: bytes,
        actor: str,
        now: int,
        expires_at: int | None = None,
    ) -> NewKey: ...

    async def list_keys(
        self, scope_id: str | None = None, *, kind: KeyKind = KeyKind.API
    ) -> tuple[KeyRecord, ...]: ...

    async def get_key(self, key_id: str) -> KeyRecord | None: ...

    async def find_key(self, prefix: str) -> StoredKey | None:
        """Lookup by the public prefix, with the stored hash; used by `keys.KeyService`."""
        ...

    async def revoke_key(self, key_id: str, *, actor: str, now: int) -> KeyRecord: ...

    async def rotate_key(
        self, key_id: str, *, grace_ms: int, pepper: bytes, actor: str, now: int
    ) -> NewKey: ...

    async def set_key_policy(
        self, policy: KeyPolicy, *, name: str | None = None, actor: str, now: int
    ) -> KeyRecord: ...

    async def find_scope(self, path: str) -> ScopeRow | None:
        """A scope by its unique path (`org:acme/user:u1`), or None. Ids never match."""
        ...

    async def get_reservation(self, request_id: str) -> ReservationRow | None: ...

    async def get_calls(self, request_id: str) -> tuple[CallRow, ...]: ...

    # Usage and retention. `retention_days` makes `usage` refuse a `since` older than
    # the configured window even before the first deletion run.
    async def usage(
        self, query: UsageQuery, *, now: int, retention_days: int | None = None
    ) -> UsageResult: ...

    async def delete_calls_older_than(
        self, days: int, now: int, batch: int = 1_000
    ) -> RetentionResult:
        """Whole UTC days of calls (plus old terminal reservations and closed sessions)."""
        ...

    # Reads and the few extra writes only the admin surface needs.
    async def global_kill_engaged(self) -> bool:
        """True while a global kill switch is active (the `/readyz` probe)."""
        ...

    async def db_now_ms(self) -> int:
        """The database clock, epoch ms. Postgres: `clock_timestamp()`. SQLite: this host's clock,
        which is the database's clock by construction (one file, one machine)."""
        ...

    async def pending_reservations(self) -> int: ...

    async def resolve_scope(self, ref: str) -> ScopeRow | None:
        """A scope by id or by unique human path."""
        ...

    async def list_scopes(
        self, *, kind: ScopeKind | None, parent_id: str | None, after: str, limit: int
    ) -> tuple[ScopeRow, ...]: ...

    async def update_scope(
        self, scope_id: str, *, display_name: str | None, actor: str, now: int
    ) -> ScopeRow: ...

    async def get_budget(self, budget_id: str, now: int) -> BudgetView | None: ...

    async def list_budgets(self, scope_id: str | None, now: int) -> tuple[BudgetView, ...]:
        """Active budgets of one scope, or of every scope when `scope_id` is None."""
        ...

    async def list_kill_switches(self) -> tuple[KillSwitchRow, ...]:
        """Active switches only, oldest first."""
        ...

    async def set_key_expiry(
        self, key_id: str, expires_at: int | None, *, actor: str, now: int
    ) -> KeyRecord: ...

    async def list_audit(
        self,
        *,
        since: int,
        until: int | None,
        actor: str | None,
        before: tuple[int, str] | None,
        limit: int,
    ) -> tuple[AuditRow, ...]:
        """Newest first; `before` is the (at, id) of the last row of the previous page."""
        ...

    async def list_calls(
        self, filters: CallFilters, *, before: CallCursor | None, limit: int
    ) -> tuple[CallFields, ...]: ...

    async def get_call(self, request_id: str) -> CallFields | None:
        """The last attempt of a request: the one that settled it."""
        ...
