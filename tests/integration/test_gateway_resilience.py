"""What keeps money correct when the surroundings misbehave: a failing settle (the sweeper is the
backstop), a crashing observer, lease heartbeats, start-up checks, and the gateway observer that
turns pipeline facts into metrics and one log line per call."""

import io
import logging
from collections.abc import Collection
from pathlib import Path

import pytest

from spendgate.core.outcomes import ReservationState
from spendgate.gateway.logs import configure_json_logging
from spendgate.gateway.metrics import Metrics
from spendgate.gateway.wiring import GatewayObserver, build_pipeline
from spendgate.ledger.port import AttemptRecord, LedgerUnavailable, Settlement
from spendgate.pipeline.pipeline import Pipeline
from tests.fakes import Script
from tests.fakes.ledger import MoneyLedgerProxy
from tests.integration.harness import async_test, open_stack


class SettleDown(MoneyLedgerProxy):
    """A ledger whose `settle` always fails (the database died after the provider answered)."""

    async def settle(
        self, request_id: str, charge_nano: int, attempts: tuple[AttemptRecord, ...], now: int
    ) -> Settlement:
        raise LedgerUnavailable("gone")


@async_test
async def test_failed_settle_leaves_the_hold_for_the_sweeper_to_charge(tmp_path: Path) -> None:
    async with open_stack(tmp_path, wrap_ledger=SettleDown) as s:
        r = await s.chat()
        assert r.status_code == 200  # the caller still gets the answer the provider billed for
        rid = r.headers["x-spendgate-request-id"]
        held = s.observer.held[0].hold.nano
        assert await s.totals() == (0, held)  # pending: still held, never silently dropped
        s.clock.advance(seconds=61)  # lease expires
        swept = await s.ledger.sweep_expired(s.clock.now())
        assert swept.expired == (rid,)
        assert await s.totals() == (held, 0)  # charged in full, at worst the hold
        row = await s.ledger.get_reservation(rid)
        assert row is not None and row.state is ReservationState.EXPIRED


@async_test
async def test_a_crashing_observer_changes_nothing(tmp_path: Path) -> None:
    class Boom:
        def on_refused(self, event: object) -> None:
            raise RuntimeError("boom")

        on_held = on_attempt = on_settled = on_refused

    async with open_stack(tmp_path, pipeline_kwargs={"observer": Boom()}) as s:
        assert (await s.chat()).status_code == 200
        assert (await s.chat("nope")).status_code == 403
        assert (await s.totals())[1] == 0


@async_test
async def test_heartbeat_renews_leases_each_interval(tmp_path: Path) -> None:
    beats: list[float] = []
    renewed: list[tuple[str, int]] = []

    class Spy(MoneyLedgerProxy):
        async def renew_all(
            self, instance: str, ids: Collection[str], now: int, lease_ms: int
        ) -> tuple[str, ...]:
            renewed.append((instance, lease_ms))
            return ()

    async def sleep(seconds: float) -> None:
        beats.append(seconds)
        if len(beats) == 3:
            raise StopAsyncIteration  # ends the loop; the real sleep would simply be cancelled

    async with open_stack(tmp_path, wrap_ledger=Spy) as s:
        with pytest.raises(StopAsyncIteration):
            await s.pipeline.heartbeat(sleep)
        assert beats == [20.0, 20.0, 20.0]  # lease 60 s / 3
        assert renewed == [(s.pipeline.instance, 60_000)] * 2  # its own unique name


@async_test
async def test_each_gateway_instance_has_its_own_lease_owner_name(tmp_path: Path) -> None:
    """Load-run defect 1: `serve` builds its pipeline through `build_pipeline`, which never passed
    `instance`, so all processes wrote owner_instance = 'gateway'. `renew_all('gateway')` of any
    live peer then kept a crashed peer's holds alive for ever and the sweeper never charged them
    (ledger-state-machine: a restarted instance gets a new owner_instance). Seen in the load run:
    117 holds still pending 25 s after kill -9 with a 4 s lease."""
    async with open_stack(tmp_path) as stack:
        sender = stack.fake.client()
        args = (stack.settings, stack.ledger, stack.keys, sender)
        first, second = build_pipeline(*args), build_pipeline(*args)
        try:
            assert first.instance != second.instance
        finally:
            await sender.aclose()


@async_test
async def test_pipeline_refuses_to_start_without_an_adapter(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        with pytest.raises(ValueError, match="no provider adapter"):
            Pipeline(
                settings=s.settings,
                ledger=s.ledger,
                keys=s.keys,
                providers={},
                sender=s.fake.client(),
            )


@async_test
async def test_gateway_observer_feeds_metrics_and_one_log_line(tmp_path: Path) -> None:
    metrics, sink = Metrics(), io.StringIO()
    logger = configure_json_logging(logging.StreamHandler(sink))
    observer = GatewayObserver(metrics, logger)
    async with open_stack(tmp_path, cooldown=1, pipeline_kwargs={"observer": observer}) as s:
        s.fake.enqueue(Script(status=429, retry_after="2"))
        ok = await s.chat()
        await s.chat("nope")
        model = "openai/gpt-6-luna"
        assert metrics.requests.value(outcome="ok", model=model) == 1
        assert metrics.requests.value(outcome="model_not_allowed", model="unknown") == 1
        assert metrics.throttles.value(model=model, deployment="alpha/0") == 1
        assert metrics.cooldowns.value(deployment="alpha/0") == 1
        assert metrics.spend_nano.value(model=model, scope="org:acme/user:u1") == 3500
        assert metrics.tokens.value(kind="input") == 10 and metrics.holds_in_flight.value() == 0
        lines = sink.getvalue().strip().splitlines()
        assert len(lines) == 1  # one line per finished call, none for the refusal
        assert '"outcome":"ok"' in lines[0] and '"cost":"0.000003500"' in lines[0]
        assert ok.status_code == 200
        assert s.secret not in sink.getvalue()
