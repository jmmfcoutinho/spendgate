"""A typed stand-in for the money port: the real ledger, with chosen methods replaced.

Tests that make the database fail, stall or count calls subclass `MoneyLedgerProxy` and override
only what they change. Every other `MoneyLedger` method forwards to the wrapped ledger, so the
double satisfies the port for mypy without `__getattr__` or `type: ignore`.
"""

from collections.abc import Collection

from spendgate.core.policy import KillSwitch
from spendgate.ledger.port import (
    AttemptRecord,
    BudgetTotals,
    Held,
    HoldRequest,
    MoneyLedger,
    Refused,
    ScopeRow,
    Settlement,
    SweepResult,
)


class MoneyLedgerProxy(MoneyLedger):
    """Forwards every `MoneyLedger` call to `inner`; subclassing the protocol makes mypy check
    that nothing is missing."""

    def __init__(self, inner: MoneyLedger) -> None:
        self.inner = inner

    async def hold(self, request: HoldRequest) -> Held | Refused:
        return await self.inner.hold(request)

    async def settle(
        self, request_id: str, charge_nano: int, attempts: tuple[AttemptRecord, ...], now: int
    ) -> Settlement:
        return await self.inner.settle(request_id, charge_nano, attempts, now)

    async def release(
        self, request_id: str, attempts: tuple[AttemptRecord, ...], now: int
    ) -> Settlement:
        return await self.inner.release(request_id, attempts, now)

    async def renew_all(
        self, instance: str, ids: Collection[str], now: int, lease_ms: int
    ) -> tuple[str, ...]:
        return await self.inner.renew_all(instance, ids, now, lease_ms)

    async def sweep_expired(self, now: int, batch: int = 100) -> SweepResult:
        return await self.inner.sweep_expired(now, batch)

    async def totals(self, scope_id: str, now: int) -> tuple[BudgetTotals, ...]:
        return await self.inner.totals(scope_id, now)

    async def get_scope(self, scope_id: str) -> ScopeRow | None:
        return await self.inner.get_scope(scope_id)

    async def active_kill_switches(self) -> tuple[KillSwitch, ...]:
        return await self.inner.active_kill_switches()
