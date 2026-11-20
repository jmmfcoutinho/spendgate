"""What the admin API and CLI read beyond `LedgerPort`, plus the plain data those reads return.

`AdminLedger` extends the money port: every backend implements it through `LedgerEngine`, so the
gateway's admin layer and the CLI's `--local` mode share one set of functions.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from spendgate.core.budgets import Budget, ScopeKind
from spendgate.core.policy import KillSwitch
from spendgate.ledger.key_types import KeyRecord
from spendgate.ledger.port import BudgetTotals, LedgerPort, ScopeRow

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


class AdminLedger(LedgerPort, Protocol):
    """Reads and the few extra writes the admin surface needs. Names follow the operation ids."""

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
