"""The server's upkeep loop: expired holds are charged and released without any request traffic,
retention runs daily, and a failing ledger never takes the loop (or the server) down (M-05)."""

import asyncio
import logging
from pathlib import Path

import pytest

from spendgate.core.outcomes import ReservationState
from spendgate.gateway.app import create_app
from spendgate.gateway.logs import JsonFormatter
from spendgate.gateway.maintenance import Maintenance
from spendgate.ledger.port import LedgerPort, SweepResult
from spendgate.ledger.usage import RetentionResult
from tests.integration.harness import async_test, open_stack
from tests.integration.stream_rig import chat_chunk
from tests.integration.test_gateway_streaming import gated, open_call

LEASE_S = 60
DAY_S = 86_400


class Spy:
    """The real ledger with counted calls and optional failures."""

    def __init__(self, inner: LedgerPort) -> None:
        self._inner = inner
        self.sweeps = 0
        self.retentions: list[int] = []
        self.fail_sweep = False
        self.fail_retention = False

    async def sweep_expired(self, now: int, batch: int = 100) -> SweepResult:
        self.sweeps += 1
        if self.fail_sweep:
            raise ConnectionError("ledger unreachable")
        return await self._inner.sweep_expired(now, batch)

    async def delete_calls_older_than(
        self, days: int, now: int, batch: int = 1_000
    ) -> RetentionResult:
        if self.fail_retention:
            raise ConnectionError("ledger unreachable")
        self.retentions.append(days)
        return await self._inner.delete_calls_older_than(days, now, batch)


class Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter())
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def logger_with(lines: Lines) -> logging.Logger:
    logger = logging.getLogger("spendgate.test-maintenance")
    logger.handlers, logger.propagate = [lines], False
    return logger


@async_test
async def test_the_loop_sweeps_a_hold_whose_lease_expired_without_any_request(
    tmp_path: Path,
) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        up.streams[0].put(chat_chunk("a"))
        held = s.observer.held[0]
        spent, hold = await s.totals()
        assert (spent, hold) == (0, held.hold.nano)  # money is held, nobody will settle it
        upkeep = Maintenance(s.ledger, s.settings, s.clock.now, logger_with(Lines()))
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 1:
                s.clock.advance(seconds=LEASE_S + 1)  # the owner went silent past its lease
            else:
                raise KeyboardInterrupt  # ends the loop after the second pass

        with pytest.raises(KeyboardInterrupt):
            await upkeep.run(sleep)
        assert sleeps == [30.0, 30.0]  # `ledger.sweep_seconds` default
        reservation = await s.ledger.get_reservation(held.request_id)
        assert reservation is not None and reservation.state is ReservationState.EXPIRED
        assert await s.totals() == (held.hold.nano, 0)  # full hold charged, nothing left held
        await call.disconnect()
        await call.finish()


@async_test
async def test_a_hold_that_is_still_leased_is_left_alone(tmp_path: Path) -> None:
    async with gated(tmp_path) as (s, _up):
        call = await open_call(s)
        upkeep = Maintenance(s.ledger, s.settings, s.clock.now, logger_with(Lines()))
        s.clock.advance(seconds=LEASE_S - 1)
        await upkeep.tick()
        held = s.observer.held[0]
        assert await s.totals() == (0, held.hold.nano)
        await call.disconnect()
        await call.finish()


@async_test
async def test_retention_runs_on_the_first_pass_then_once_a_day(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        spy = Spy(s.ledger)
        upkeep = Maintenance(spy, s.settings, s.clock.now, logger_with(Lines()))  # type: ignore[arg-type]
        await upkeep.tick()
        s.clock.advance(seconds=3_600)
        await upkeep.tick()
        assert spy.retentions == [90]  # `ledger.retention_days`, once
        s.clock.advance(seconds=DAY_S)
        await upkeep.tick()
        assert spy.retentions == [90, 90]
        assert spy.sweeps == 3  # the sweeper runs on every pass


@async_test
async def test_ledger_errors_are_logged_never_raised_and_retried(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        spy, lines = Spy(s.ledger), Lines()
        spy.fail_sweep = spy.fail_retention = True
        upkeep = Maintenance(spy, s.settings, s.clock.now, logger_with(lines))  # type: ignore[arg-type]
        await upkeep.tick()  # must not raise
        events = [line for line in lines.lines]
        assert any('"sweep_failed"' in e for e in events)
        assert any('"retention_failed"' in e for e in events)
        assert all("unreachable" not in e for e in events)  # no exception text in the log line
        spy.fail_sweep = spy.fail_retention = False
        await upkeep.tick()  # the next pass retries, including the retention that failed
        assert spy.retentions == [90]


@async_test
async def test_the_app_starts_the_background_job_with_it_and_cancels_it_on_shutdown(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path) as s:
        state = {"running": False, "cancelled": False}

        async def job() -> None:
            state["running"] = True
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        app = create_app(s.pipeline, background=job)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)  # let the job task start
            assert state == {"running": True, "cancelled": False}
        assert state["cancelled"]
