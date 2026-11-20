"""A cancelled client is still settled (T-MON-11, M-13, M-12): after send the full hold, before
send a release at 0. The pipeline settles from a task a cancel cannot interrupt."""

import asyncio
from pathlib import Path

import httpx

from spendgate.core.outcomes import Outcome, ReservationState
from spendgate.ledger.port import HoldRequest, LedgerPort, Settlement
from tests.fakes import Script
from tests.integration.harness import Stack, async_test, open_stack, wait_until


class Gated:
    """Wraps a ledger: `hold` and `settle` commit, then wait for the test to let them return."""

    def __init__(
        self, inner: LedgerPort, *, gate_hold: bool = False, gate_settle: bool = False
    ) -> None:
        self._inner = inner
        self._gate_hold, self._gate_settle = gate_hold, gate_settle
        self.request_id = ""
        self.hold_done = asyncio.Event()
        self.settle_started = asyncio.Event()
        self.proceed = asyncio.Event()

    async def hold(self, request: HoldRequest) -> object:
        self.request_id = request.request_id
        result = await self._inner.hold(request)
        self.hold_done.set()
        if self._gate_hold:
            await self.proceed.wait()
        return result

    async def settle(self, *args: object) -> Settlement:
        self.settle_started.set()
        if self._gate_settle:
            await self.proceed.wait()
        return await self._inner.settle(*args)  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


async def start_hanging_call(s: Stack) -> asyncio.Future[httpx.Response]:
    s.fake.enqueue(Script(hang=True))
    call = asyncio.ensure_future(s.chat())
    await wait_until(lambda: s.fake.calls >= 1)  # the request is on the wire
    return call


@async_test
async def test_cancel_after_send_is_charged_the_full_hold(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        call = await start_hanging_call(s)
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        await s.pipeline.aclose()
        held = s.observer.held[0]
        assert await s.totals() == (held.hold.nano, 0)
        rid = held.request_id
        calls = await s.ledger.get_calls(rid)
        assert [(c.outcome, c.charged_nano) for c in calls] == [(Outcome.CANCELLED, held.hold.nano)]
        reservation = await s.ledger.get_reservation(rid)
        assert reservation is not None and reservation.state is ReservationState.SETTLED
        assert s.fake.cancelled == 1  # the upstream call was torn down too


@async_test
async def test_cancel_before_send_releases_the_hold(tmp_path: Path) -> None:
    """The hold committed but the upstream request was never built: released, nothing charged."""
    gate: list[Gated] = []

    def wrap(ledger: LedgerPort) -> LedgerPort:
        gate.append(Gated(ledger, gate_hold=True))
        return gate[0]  # type: ignore[return-value]

    async with open_stack(tmp_path, wrap_ledger=wrap) as s:
        call = asyncio.ensure_future(s.chat())
        await gate[0].hold_done.wait()  # committed in the database, not yet seen by the pipeline
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        gate[0].proceed.set()
        await s.pipeline.aclose()
        s.assert_no_contact()
        assert await s.totals() == (0, 0)
        reservation = await s.ledger.get_reservation(gate[0].request_id)
        assert reservation is not None and reservation.state is ReservationState.RELEASED
        assert await s.ledger.get_calls(gate[0].request_id) == ()  # released: no row


@async_test
async def test_second_cancel_cannot_interrupt_the_settle(tmp_path: Path) -> None:
    gate: list[Gated] = []

    def wrap(ledger: LedgerPort) -> LedgerPort:
        gate.append(Gated(ledger, gate_settle=True))
        return gate[0]  # type: ignore[return-value]

    async with open_stack(tmp_path, wrap_ledger=wrap) as s:
        call = await start_hanging_call(s)
        call.cancel()
        await gate[0].settle_started.wait()  # the cancel-triggered settle is under way
        call.cancel()  # a second cancel lands while it waits
        await asyncio.gather(call, return_exceptions=True)
        gate[0].proceed.set()
        await s.pipeline.aclose()
        held = s.observer.held[0]
        assert await s.totals() == (held.hold.nano, 0)  # still booked exactly once
        assert len(await s.ledger.get_calls(held.request_id)) == 1
