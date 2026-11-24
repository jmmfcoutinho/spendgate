"""Ledger and gateway health metrics (issue #34): each series moves on the event it names.

Hold and settle latency, ledger errors and `ledger_unavailable` refusals, the pool, sweeper
expirations, closes the ledger has not taken, heartbeat failures and gateway overhead. Labels are
fixed sets in the code (`op`, `step`), never anything from the request.
"""

import asyncio
import io
import logging
from collections.abc import Awaitable, Callable, Collection
from pathlib import Path

import pytest

from spendgate.core.money import Money
from spendgate.core.outcomes import Outcome
from spendgate.gateway.logs import configure_json_logging
from spendgate.gateway.maintenance import Maintenance
from spendgate.gateway.metrics import Metrics
from spendgate.gateway.wiring import GatewayObserver, PooledLedger, bind_runtime_metrics
from spendgate.ledger.port import (
    AttemptRecord,
    Held,
    HoldRequest,
    LedgerUnavailable,
    MoneyLedger,
    PoolStats,
    Refused,
    Settlement,
)
from spendgate.pipeline.model import LedgerEvent, SettledEvent
from tests.fakes import FakeUpstream
from tests.fakes.ledger import MoneyLedgerProxy
from tests.fakes.script import Script
from tests.integration.harness import Stack, async_test, open_stack
from tests.integration.test_gateway_refusals import Stalled
from tests.integration.test_maintenance import Lines, Spy, logger_with

LEASE_S = 60


def metered() -> tuple[Metrics, GatewayObserver]:
    metrics = Metrics()
    logger = configure_json_logging(logging.StreamHandler(io.StringIO()))
    return metrics, GatewayObserver(metrics, logger)


class Flaky(MoneyLedgerProxy):
    """The real ledger, with chosen money calls failing as an unreachable database would."""

    def __init__(self, inner: MoneyLedger) -> None:
        super().__init__(inner)
        self.fail_hold = self.fail_settle = self.fail_renew = False

    async def hold(self, request: HoldRequest) -> Held | Refused:
        if self.fail_hold:
            raise LedgerUnavailable("ledger error: connection refused")
        return await self.inner.hold(request)

    async def settle(
        self, request_id: str, charge_nano: int, attempts: tuple[AttemptRecord, ...], now: int
    ) -> Settlement:
        if self.fail_settle:
            raise LedgerUnavailable("ledger error: connection refused")
        return await self.inner.settle(request_id, charge_nano, attempts, now)

    async def renew_all(
        self, instance: str, ids: Collection[str], now: int, lease_ms: int
    ) -> tuple[str, ...]:
        if self.fail_renew:
            raise LedgerUnavailable("ledger error: connection refused")
        return await self.inner.renew_all(instance, ids, now, lease_ms)


def flaky_rig() -> tuple[list[Flaky], Callable[[MoneyLedger], MoneyLedger]]:
    made: list[Flaky] = []

    def wrap(ledger: MoneyLedger) -> MoneyLedger:
        made.append(Flaky(ledger))
        return made[0]

    return made, wrap


async def no_wait(_seconds: float) -> None:
    return None


# --- hold and settle latency, ledger errors -----------------------------------------------------


@async_test
async def test_a_call_times_its_hold_and_its_settle_and_its_overhead(tmp_path: Path) -> None:
    metrics, obs = metered()
    async with open_stack(tmp_path, pipeline_kwargs={"observer": obs}) as s:
        assert (await s.chat()).status_code == 200
        await s.pipeline.drain()
    assert metrics.ledger_latency.count(op="hold") == 1
    assert metrics.ledger_latency.count(op="settle") == 1
    assert metrics.ledger_latency.count(op="release") == 0
    assert metrics.overhead.count() == 1
    assert metrics.ledger_errors.value(op="hold") == metrics.ledger_errors.value(op="settle") == 0


@async_test
async def test_a_call_that_never_reached_the_provider_times_its_release(tmp_path: Path) -> None:
    metrics, obs = metered()
    throttled = FakeUpstream(Script(status=429))  # free status: every attempt not_processed
    async with open_stack(tmp_path, fake=throttled, pipeline_kwargs={"observer": obs}) as s:
        assert (await s.chat()).status_code == 429
        await s.pipeline.drain()
    assert metrics.ledger_latency.count(op="release") == 1
    assert metrics.ledger_latency.count(op="settle") == 0


@async_test
async def test_a_budget_refusal_is_a_timed_hold_not_a_ledger_error(tmp_path: Path) -> None:
    metrics, obs = metered()
    async with open_stack(tmp_path, budget="0.000001", pipeline_kwargs={"observer": obs}) as s:
        reply = await s.chat()
        assert reply.json()["error"]["code"] == "budget_exhausted"
        s.assert_no_contact()
    assert metrics.ledger_latency.count(op="hold") == 1
    assert metrics.ledger_errors.value(op="hold") == 0
    assert metrics.ledger_unavailable.value() == 0


@async_test
async def test_m12_a_failed_hold_counts_a_ledger_error_and_a_ledger_unavailable_refusal(
    tmp_path: Path,
) -> None:
    metrics, obs = metered()
    made, wrap = flaky_rig()
    async with open_stack(tmp_path, wrap_ledger=wrap, pipeline_kwargs={"observer": obs}) as s:
        made[0].fail_hold = True
        reply = await s.chat()
        assert reply.status_code == 503 and reply.json()["error"]["code"] == "ledger_unavailable"
        s.assert_no_contact()
    assert metrics.ledger_errors.value(op="hold") == 1
    assert metrics.ledger_unavailable.value() == 1
    assert metrics.ledger_latency.count(op="hold") == 1


@async_test
async def test_m12_a_hold_past_its_deadline_counts_as_a_ledger_error(tmp_path: Path) -> None:
    metrics, obs = metered()
    stalled: list[Stalled] = []

    def wrap(ledger: MoneyLedger) -> MoneyLedger:
        stalled.append(Stalled(ledger))
        return stalled[0]

    quick: dict[str, object] = {"hold_seconds": 0.05, "observer": obs}
    async with open_stack(tmp_path, wrap_ledger=wrap, pipeline_kwargs=quick) as s:
        try:
            reply = await asyncio.wait_for(s.chat(), 5)
        finally:
            stalled[0].recover.set()
        assert reply.json()["error"]["code"] == "ledger_unavailable"
        s.assert_no_contact()
        await s.pipeline.aclose()  # the late hold is given back (released), not a second error
    assert metrics.ledger_errors.value(op="hold") == 1
    assert metrics.ledger_unavailable.value() == 1


@async_test
async def test_a_settle_the_ledger_refuses_counts_each_try_and_stays_unsettled_until_swept(
    tmp_path: Path,
) -> None:
    metrics, obs = metered()
    made, wrap = flaky_rig()
    kwargs: dict[str, object] = {"observer": obs, "sleep": no_wait}
    async with open_stack(tmp_path, wrap_ledger=wrap, pipeline_kwargs=kwargs) as s:
        bind_runtime_metrics(metrics, s.pipeline.unsettled, made[0])
        made[0].fail_settle = True
        assert (await s.chat()).status_code == 200  # answered; its close is left to the backstop
        await s.pipeline.drain()
        assert metrics.ledger_errors.value(op="settle") == 3  # every try of the one close
        assert "spendgate_unsettled_closes 1" in metrics.render().splitlines()
        upkeep = Maintenance(s.ledger, s.settings, s.clock.now, logger_with(Lines()), metrics)
        await upkeep.tick()
        assert metrics.sweeper_expired.value() == 0  # the lease has not run out yet
        s.clock.advance(seconds=LEASE_S + 1)  # never renewed: the close is not taken
        await upkeep.tick()
        assert metrics.sweeper_expired.value() == 1  # charged in full by the sweeper


@async_test
async def test_a_failed_lease_renewal_counts_a_renew_error(tmp_path: Path) -> None:
    metrics, obs = metered()
    made, wrap = flaky_rig()
    async with open_stack(tmp_path, wrap_ledger=wrap, pipeline_kwargs={"observer": obs}) as s:
        await s.pipeline.renew_leases()
        assert metrics.ledger_errors.value(op="renew") == 0
        made[0].fail_renew = True
        await s.pipeline.renew_leases()
    assert metrics.ledger_errors.value(op="renew") == 1


@async_test
async def test_failed_sweeps_and_retention_count_ledger_errors(tmp_path: Path) -> None:
    metrics = Metrics()
    async with open_stack(tmp_path) as s:
        spy = Spy(s.ledger)
        spy.fail_sweep = spy.fail_retention = True
        upkeep = Maintenance(spy, s.settings, s.clock.now, logger_with(Lines()), metrics)
        await upkeep.tick()
    assert metrics.ledger_errors.value(op="sweep") == 1
    assert metrics.ledger_errors.value(op="retention") == 1


@async_test
async def test_an_observer_that_raises_on_ledger_events_changes_no_money(tmp_path: Path) -> None:
    class Broken(GatewayObserver):
        def on_ledger(self, event: LedgerEvent) -> None:
            raise RuntimeError("metrics bug")

    metrics, _ = metered()
    obs = Broken(metrics, logging.getLogger("spendgate.test-broken"))
    async with open_stack(tmp_path, pipeline_kwargs={"observer": obs}) as s:
        assert (await s.chat()).status_code == 200
        await s.pipeline.drain()
        spent, held = await s.totals()
        assert spent > 0 and held == 0
        assert s.pipeline.unsettled() == frozenset() and s.pipeline.owned() == frozenset()


# --- heartbeat ---------------------------------------------------------------------------------


def beats(count: int) -> Callable[[float], Awaitable[None]]:
    """A heartbeat `sleep` that returns at once `count` times, then cancels the loop."""
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        if len(slept) == count:
            raise asyncio.CancelledError
        slept.append(seconds)

    return sleep


@async_test
async def test_a_failing_heartbeat_step_counts_one_failure_per_beat(tmp_path: Path) -> None:
    metrics, obs = metered()

    async def boom() -> None:
        raise ConnectionError("ledger unreachable")

    async with open_stack(tmp_path, pipeline_kwargs={"observer": obs}) as s:
        steps = [("sweep", boom), ("made_up_step", boom)]
        with pytest.raises(asyncio.CancelledError):  # how the stub sleep ends the loop
            await s.pipeline.heartbeat(beats(2), also=steps)
    assert metrics.heartbeat_failures.value(step="sweep") == 2
    assert metrics.heartbeat_failures.value(step="other") == 2  # an unknown name is folded
    assert metrics.heartbeat_failures.value(step="renew_leases") == 0


@async_test
async def test_an_observer_that_raises_never_stops_the_heartbeat(tmp_path: Path) -> None:
    class Broken(GatewayObserver):
        def on_heartbeat_failed(self, step: str) -> None:
            raise RuntimeError("metrics bug")

    calls: list[int] = []

    async def boom() -> None:
        calls.append(1)
        raise ConnectionError("ledger unreachable")

    metrics, _ = metered()
    obs = Broken(metrics, logging.getLogger("spendgate.test-broken"))
    async with open_stack(tmp_path, pipeline_kwargs={"observer": obs}) as s:
        with pytest.raises(asyncio.CancelledError):  # how the stub sleep ends the loop
            await s.pipeline.heartbeat(beats(3), also=[("sweep", boom)])
    assert len(calls) == 3


# --- overhead ----------------------------------------------------------------------------------


@async_test
async def test_gateway_overhead_leaves_out_the_time_spent_upstream(tmp_path: Path) -> None:
    metrics, obs = metered()
    box: list[Stack] = []

    async def upstream_takes(seconds: float) -> None:
        box[0].clock.advance(ms=int(seconds * 1000))  # time passes only inside the provider

    fake = FakeUpstream(Script(latency_ms=250), delay=upstream_takes)
    async with open_stack(tmp_path, fake=fake, pipeline_kwargs={"observer": obs}) as s:
        box.append(s)
        assert (await s.chat()).status_code == 200
        await s.pipeline.drain()
    lines = metrics.render().splitlines()
    assert "spendgate_gateway_overhead_ms_count 1" in lines
    assert "spendgate_gateway_overhead_ms_sum 0" in lines
    assert (
        'spendgate_upstream_latency_ms_sum{model="openai/gpt-6-luna",deployment="alpha/0"} 250'
        in (lines)
    )


def settled(latency_ms: int, upstream_ms: int) -> SettledEvent:
    return SettledEvent(
        "sg_req_1", "k1234567", "s", "/v1/chat/completions", "m", "d", Outcome.OK,
        Money(5, "USD"), Money(9, "USD"), None, latency_ms, False, upstream_ms=upstream_ms,
    )  # fmt: skip


def test_overhead_is_the_call_latency_minus_the_upstream_time_never_negative() -> None:
    metrics, obs = metered()
    obs.on_settled(settled(120, 100))
    obs.on_settled(settled(50, 80))  # clock steps can make upstream look longer: clamped
    lines = metrics.render().splitlines()
    assert "spendgate_gateway_overhead_ms_sum 20" in lines
    assert "spendgate_gateway_overhead_ms_count 2" in lines


def test_ledger_operations_are_a_fixed_label_set() -> None:
    metrics, obs = metered()
    obs.on_ledger(LedgerEvent("settle", 3, ok=False))
    obs.on_ledger(LedgerEvent("sg_req_made_up", 3, ok=False))
    assert metrics.ledger_errors.value(op="settle") == 1
    assert metrics.ledger_errors.value(op="other") == 1
    assert "sg_req_made_up" not in metrics.render()


# --- the pool -----------------------------------------------------------------------------------


class FakePooled:
    def __init__(self, stats: PoolStats | None) -> None:
        self.pool_wait: Callable[[float], None] | None = None
        self.stats = stats

    def pool_stats(self) -> PoolStats | None:
        return self.stats


def test_pool_gauges_and_wait_histogram_follow_the_pooled_ledger() -> None:
    metrics = Metrics()
    ledger = FakePooled(None)
    assert isinstance(ledger, PooledLedger)
    bind_runtime_metrics(metrics, lambda: frozenset({"sg_req_1", "sg_req_2"}), ledger)
    lines = metrics.render().splitlines()
    assert "spendgate_unsettled_closes 2" in lines
    assert not any(line.startswith("spendgate_ledger_pool_size ") for line in lines)  # not open
    ledger.stats = PoolStats(size=4, in_use=3, max_size=10)
    assert ledger.pool_wait is not None
    ledger.pool_wait(7.5)
    lines = metrics.render().splitlines()
    assert "spendgate_ledger_pool_size 4" in lines
    assert "spendgate_ledger_pool_in_use 3" in lines
    assert "spendgate_ledger_pool_max 10" in lines
    assert "spendgate_ledger_pool_wait_ms_count 1" in lines


@async_test
async def test_sqlite_has_no_pool_series(tmp_path: Path) -> None:
    metrics = Metrics()
    async with open_stack(tmp_path) as s:
        assert not isinstance(s.ledger, PooledLedger)
        bind_runtime_metrics(metrics, s.pipeline.unsettled, s.ledger)
        text = metrics.render()
    assert "spendgate_unsettled_closes 0" in text.splitlines()
    assert not any(line.startswith("spendgate_ledger_pool_") for line in text.splitlines()
                   if not line.startswith("#"))  # fmt: skip


def test_a_reader_that_raises_does_not_break_the_scrape() -> None:
    metrics = Metrics()

    def broken() -> None:
        raise RuntimeError("probe bug")

    metrics.on_render(broken)
    assert "spendgate_holds_in_flight 0" in metrics.render().splitlines()
