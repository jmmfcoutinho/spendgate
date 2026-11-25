"""The server's background upkeep of the ledger: the sweeper and daily retention.

The sweeper is the money backstop: a hold whose owner died is charged in full and released only
by `sweep_expired`, so a server that never calls it leaves budgets blocked for good. Retention
deletes whole UTC days of old calls once a day. Neither may take the server down: every pass
catches its errors, logs one event and tries again at the next interval.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from spendgate.config import Settings
from spendgate.gateway.logs import log_event
from spendgate.gateway.metrics import Metrics
from spendgate.ledger.port import SweepResult
from spendgate.ledger.usage import RetentionResult
from spendgate.pipeline.model import Clock

_DAY_MS = 86_400_000
_SWEEP_BATCH = 100


class MaintainedLedger(Protocol):
    """The two ledger calls upkeep makes; `AdminLedger` has both."""

    async def sweep_expired(self, now: int, batch: int = 100) -> SweepResult: ...

    async def delete_calls_older_than(
        self, days: int, now: int, batch: int = 1_000
    ) -> RetentionResult: ...


class Maintenance:
    def __init__(
        self, ledger: MaintainedLedger, settings: Settings, now: Clock, logger: logging.Logger,
        metrics: Metrics | None = None,
    ) -> None:  # fmt: skip
        self._ledger, self._settings, self._now, self._logger = ledger, settings, now, logger
        self._metrics = metrics
        self._retention_due = 0  # epoch ms; the first pass runs it, then once a day

    async def tick(self) -> None:
        """One pass: sweep every expired hold, then retention if a day has passed."""
        await self._guarded("sweep_failed", "sweep", self._sweep)
        if self._now() >= self._retention_due:
            await self._guarded("retention_failed", "retention", self._retain)

    async def run(self, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        """Tick every `ledger.sweep_seconds` until cancelled."""
        interval = self._settings.sweep_ms / 1000
        while True:
            await self.tick()
            await sleep(interval)

    async def _sweep(self) -> None:
        while True:
            expired = len((await self._ledger.sweep_expired(self._now(), _SWEEP_BATCH)).expired)
            if self._metrics is not None:
                self._metrics.sweeper_expired.inc(expired)  # each one charged in full
            if expired < _SWEEP_BATCH:  # a full batch means more may be waiting
                return

    async def _retain(self) -> None:
        now = self._now()
        await self._ledger.delete_calls_older_than(self._settings.config.ledger.retention_days, now)
        self._retention_due = now + _DAY_MS  # set after success: a failure retries next tick

    async def _guarded(self, event: str, op: str, step: Callable[[], Awaitable[None]]) -> None:
        try:
            await step()
        except Exception as exc:  # the server keeps serving; the next tick tries again
            log_event(self._logger, event, logging.ERROR, reason=type(exc).__name__)
            if self._metrics is not None:
                self._metrics.ledger_errors.inc(op=op)
