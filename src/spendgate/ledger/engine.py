"""The backend-independent half of a ledger: port methods expressed as flows.

A backend subclasses `LedgerEngine` and implements one method, `_run`, which executes a flow
in a transaction (SQLite: a worker thread with `BEGIN IMMEDIATE`; Postgres: an async pool with
deadlock retry). Every money decision lives in the flows, so both backends behave alike.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Collection
from functools import partial

from spendgate.core.budgets import Budget, Period, ScopeKind
from spendgate.core.policy import KeyPolicy, KillSwitch
from spendgate.ledger import admin_flow as admin
from spendgate.ledger import admin_reads, key_flow, usage_flow
from spendgate.ledger.admin_engine import AdminReadsMixin
from spendgate.ledger.common import Ctx
from spendgate.ledger.hold_flow import hold_flow
from spendgate.ledger.ids import new_id as default_new_id
from spendgate.ledger.key_types import AdminScope, KeyKind, KeyRecord, KeySpec, NewKey, StoredKey
from spendgate.ledger.port import (
    AttemptRecord,
    BudgetTotals,
    CallRow,
    Held,
    HoldRequest,
    Refused,
    ReservationRow,
    ScopeRow,
    Settlement,
    SweepResult,
)
from spendgate.ledger.settle_flow import (
    due_flow,
    expire_flow,
    release_flow,
    renew_flow,
    settle_flow,
)
from spendgate.ledger.sql import Flow
from spendgate.ledger.usage import RetentionResult, UsageQuery, UsageResult, retention_boundary


class LedgerEngine(AdminReadsMixin, ABC):
    def __init__(self, currency: str = "USD", new_id: Callable[[], str] = default_new_id) -> None:
        self._ctx = Ctx(currency, new_id)

    @abstractmethod
    async def _run[T](
        self, make_flow: Callable[[], Flow[T]], *, write: bool = True, slow: bool = False
    ) -> T:
        """Run a fresh flow in one transaction. A factory, so a backend may retry it.

        `slow` marks a report or batch over the call tables (usage, call and audit lists,
        retention) that may legitimately run longer than a money transition.
        """

    # --- money transitions ---

    async def hold(self, request: HoldRequest) -> Held | Refused:
        return await self._run(partial(hold_flow, self._ctx, request))

    async def settle(
        self, request_id: str, charge_nano: int, attempts: tuple[AttemptRecord, ...], now: int
    ) -> Settlement:
        return await self._run(partial(settle_flow, request_id, charge_nano, attempts, now))

    async def release(
        self, request_id: str, attempts: tuple[AttemptRecord, ...], now: int
    ) -> Settlement:
        return await self._run(partial(release_flow, request_id, attempts, now))

    async def renew_all(
        self, instance: str, ids: Collection[str], now: int, lease_ms: int
    ) -> tuple[str, ...]:
        if not ids:
            return ()
        return await self._run(partial(renew_flow, instance, tuple(sorted(ids)), now + lease_ms))

    async def sweep_expired(self, now: int, batch: int = 100) -> SweepResult:
        """The only path that charges expired holds: one transaction per reservation."""
        due = await self._run(partial(due_flow, now, batch), write=False)
        expired = [r for r in due if await self._run(partial(expire_flow, r, now))]
        changed = await self._run(partial(admin.grant_expiry_flow, now))
        return SweepResult(tuple(expired), tuple(changed))

    # --- admin side ---

    async def create_scope(  # noqa: PLR0913 - mirrors the admin ScopeCreate body
        self,
        kind: ScopeKind,
        name: str,
        parent_id: str | None,
        *,
        now: int,
        actor: str = "system",
        display_name: str | None = None,
    ) -> ScopeRow:
        flow = partial(
            admin.create_scope_flow,
            self._ctx,
            kind,
            name,
            parent_id,
            now,
            actor=actor,
            display_name=display_name,
        )
        return await self._run(flow)

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
    ) -> Budget:
        flow = partial(
            admin.create_budget_flow,
            self._ctx,
            scope_id=scope_id,
            period=period,
            cap_nano=cap_nano,
            now=now,
            soft_pct=soft_pct,
            model_filter=model_filter,
            strict_overrun=strict_overrun,
            note=note,
            actor=actor,
            budget_id=budget_id,
        )
        return await self._run(flow)

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
    ) -> Budget:
        flow = partial(
            admin.update_budget_flow,
            self._ctx,
            budget_id=budget_id,
            actor=actor,
            now=now,
            cap_nano=cap_nano,
            soft_pct=soft_pct,
            strict_overrun=strict_overrun,
            note=note,
        )
        return await self._run(flow)

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
    ) -> str:
        flow = partial(
            admin.create_grant_flow,
            self._ctx,
            budget_id=budget_id,
            amount_nano=amount_nano,
            valid_from=valid_from,
            valid_until=valid_until,
            actor=actor,
            reason=reason,
            now=now,
        )
        return await self._run(flow)

    async def ack_overrun(self, budget_id: str, *, actor: str, reason: str, now: int) -> None:
        await self._run(partial(admin.ack_overrun_flow, self._ctx, budget_id, actor, reason, now))

    async def engage_kill_switch(
        self, switch: KillSwitch, *, actor: str, reason: str, now: int
    ) -> str:
        flow = partial(admin.engage_kill_switch_flow, self._ctx, switch, actor, reason, now)
        return await self._run(flow)

    async def lift_kill_switch(self, switch_id: str, *, actor: str, now: int) -> None:
        await self._run(partial(admin.lift_kill_switch_flow, self._ctx, switch_id, actor, now))

    async def delete_budget(self, budget_id: str, *, actor: str, now: int) -> None:
        await self._run(partial(admin.delete_budget_flow, self._ctx, budget_id, actor, now))

    async def disable_scope(self, scope_id: str, *, actor: str, now: int) -> None:
        await self._run(partial(admin.disable_scope_flow, self._ctx, scope_id, actor, now))

    async def add_key_record(self, key_id: str, scope_id: str, *, now: int) -> None:
        await self._run(partial(admin.add_key_record_flow, key_id, scope_id, now))

    # --- keys ---

    async def create_key(self, spec: KeySpec, *, pepper: bytes, actor: str, now: int) -> NewKey:
        return await self._run(
            partial(key_flow.create_key_flow, self._ctx, spec, pepper, actor, now)
        )

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
    ) -> NewKey:
        spec = KeySpec(
            scope_id, name, KeyKind.ADMIN, admin_scopes=admin_scopes, expires_at=expires_at
        )
        return await self.create_key(spec, pepper=pepper, actor=actor, now=now)

    async def list_keys(
        self, scope_id: str | None = None, *, kind: KeyKind = KeyKind.API
    ) -> tuple[KeyRecord, ...]:
        return await self._run(partial(key_flow.list_flow, kind, scope_id), write=False)

    async def get_key(self, key_id: str) -> KeyRecord | None:
        return await self._run(partial(key_flow.get_flow, key_id), write=False)

    async def find_key(self, prefix: str) -> StoredKey | None:
        return await self._run(partial(key_flow.find_flow, prefix), write=False)

    async def revoke_key(self, key_id: str, *, actor: str, now: int) -> KeyRecord:
        return await self._run(partial(key_flow.revoke_flow, self._ctx, key_id, actor, now))

    async def rotate_key(
        self, key_id: str, *, grace_ms: int, pepper: bytes, actor: str, now: int
    ) -> NewKey:
        flow = partial(key_flow.rotate_flow, self._ctx, key_id, grace_ms, pepper, actor, now)
        return await self._run(flow)

    async def set_key_policy(
        self, policy: KeyPolicy, *, name: str | None = None, actor: str, now: int
    ) -> KeyRecord:
        flow = partial(key_flow.set_policy_flow, self._ctx, policy, name, actor, now)
        return await self._run(flow)

    # --- reads ---

    async def totals(self, scope_id: str, now: int) -> tuple[BudgetTotals, ...]:
        return await self._run(partial(admin.totals_flow, scope_id, now), write=False)

    async def get_scope(self, scope_id: str) -> ScopeRow | None:
        return await self._run(partial(admin.get_scope_flow, scope_id), write=False)

    async def active_kill_switches(self) -> tuple[KillSwitch, ...]:
        return await self._run(admin_reads.active_kill_switches_flow, write=False)

    async def find_scope(self, path: str) -> ScopeRow | None:
        return await self._run(partial(admin_reads.find_scope_flow, path), write=False)

    async def get_reservation(self, request_id: str) -> ReservationRow | None:
        return await self._run(partial(admin_reads.get_reservation_flow, request_id), write=False)

    async def get_calls(self, request_id: str) -> tuple[CallRow, ...]:
        return await self._run(partial(admin_reads.get_calls_flow, request_id), write=False)

    # --- usage and retention ---

    async def usage(
        self, query: UsageQuery, *, now: int, retention_days: int | None = None
    ) -> UsageResult:
        flow = partial(usage_flow.usage_flow, query, self._ctx.currency, now, retention_days)
        return await self._run(flow, write=False, slow=True)

    async def delete_calls_older_than(
        self, days: int, now: int, batch: int = 1_000
    ) -> RetentionResult:
        """Delete whole UTC days older than `days`, `batch` rows per transaction."""
        boundary = retention_boundary(now, days)
        await self._run(partial(usage_flow.boundary_flow, boundary), slow=True)
        calls = await self._drain(usage_flow.calls_batch_flow, boundary, batch)
        reservations = await self._drain(usage_flow.reservations_batch_flow, boundary, batch)
        sessions = await self._drain(usage_flow.sessions_batch_flow, boundary, batch)
        return RetentionResult(boundary, calls, reservations, sessions)

    async def _drain(self, flow: Callable[[int, int], Flow[int]], boundary: int, batch: int) -> int:
        """Repeat a batch transaction until it comes back short; returns the rows removed."""
        total = 0
        while (done := await self._run(partial(flow, boundary, batch), slow=True)) > 0:
            total += done
            if done < batch:
                break
        return total
