"""`AdminLedger` methods as flows, mixed into `LedgerEngine`."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from functools import partial

from spendgate.core.budgets import ScopeKind
from spendgate.ledger import admin_flow, admin_reads
from spendgate.ledger.admin_port import (
    AuditRow,
    BudgetView,
    CallCursor,
    CallFields,
    CallFilters,
    KillSwitchRow,
)
from spendgate.ledger.common import Ctx
from spendgate.ledger.key_types import KeyRecord
from spendgate.ledger.port import ScopeRow
from spendgate.ledger.sql import Flow


class AdminReadsMixin(ABC):
    _ctx: Ctx

    @abstractmethod
    async def _run[T](self, make_flow: Callable[[], Flow[T]], *, write: bool = True) -> T: ...

    @abstractmethod
    async def db_now_ms(self) -> int: ...

    async def global_kill_engaged(self) -> bool:
        return await self._run(admin_reads.global_kill_flow, write=False)

    async def pending_reservations(self) -> int:
        return await self._run(admin_reads.pending_flow, write=False)

    async def resolve_scope(self, ref: str) -> ScopeRow | None:
        return await self._run(partial(admin_reads.resolve_scope_flow, ref), write=False)

    async def list_scopes(
        self, *, kind: ScopeKind | None, parent_id: str | None, after: str, limit: int
    ) -> tuple[ScopeRow, ...]:
        flow = partial(admin_reads.list_scopes_flow, kind, parent_id, after, limit)
        return await self._run(flow, write=False)

    async def update_scope(
        self, scope_id: str, *, display_name: str | None, actor: str, now: int
    ) -> ScopeRow:
        flow = partial(admin_flow.update_scope_flow, self._ctx, scope_id, display_name, actor, now)
        return await self._run(flow)

    async def get_budget(self, budget_id: str, now: int) -> BudgetView | None:
        return await self._run(partial(admin_reads.get_budget_flow, budget_id, now), write=False)

    async def list_budgets(self, scope_id: str | None, now: int) -> tuple[BudgetView, ...]:
        return await self._run(partial(admin_reads.list_budgets_flow, scope_id, now), write=False)

    async def list_kill_switches(self) -> tuple[KillSwitchRow, ...]:
        return await self._run(admin_reads.list_kill_switches_flow, write=False)

    async def set_key_expiry(
        self, key_id: str, expires_at: int | None, *, actor: str, now: int
    ) -> KeyRecord:
        flow = partial(admin_reads.set_key_expiry_flow, self._ctx, key_id, expires_at, actor, now)
        return await self._run(flow)

    async def list_audit(
        self,
        *,
        since: int,
        until: int | None,
        actor: str | None,
        before: tuple[int, str] | None,
        limit: int,
    ) -> tuple[AuditRow, ...]:
        flow = partial(admin_reads.list_audit_flow, since, until, actor, before, limit)
        return await self._run(flow, write=False)

    async def list_calls(
        self, filters: CallFilters, *, before: CallCursor | None, limit: int
    ) -> tuple[CallFields, ...]:
        flow = partial(admin_reads.list_calls_flow, filters, before, limit)
        return await self._run(flow, write=False)

    async def get_call(self, request_id: str) -> CallFields | None:
        return await self._run(partial(admin_reads.get_call_flow, request_id), write=False)
