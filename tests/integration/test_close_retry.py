"""Closing a hold when the ledger fails at the wrong moment: retried by the heartbeat, else swept.

A settle or release the ledger refused is kept as unsettled and retried by the next heartbeat;
its lease is no longer renewed, so if the retries never land the sweeper charges the full hold. A
hold whose COMMIT reply was lost is given back. Each wrapper below is kept by the test, which flips
its switch to bring the database back.
"""

import asyncio
from pathlib import Path

from spendgate.core.outcomes import ReservationState
from spendgate.ledger.port import (
    AttemptRecord,
    Held,
    HoldRequest,
    LedgerUnavailable,
    MoneyLedger,
    Refused,
    Settlement,
)
from spendgate.ledger.sqlite import SqliteLedger
from tests.fakes.ledger import MoneyLedgerProxy
from tests.integration.harness import Stack, async_test, open_stack


async def no_sleep(seconds: float) -> None:
    return None


class Partial(MoneyLedgerProxy):
    """A partial stand-in ledger: `wrap` is the `wrap_ledger` hook, the rest is the real one."""

    def __init__(self) -> None:
        """No ledger yet: the stack hands it over through `wrap`."""

    def wrap(self, inner: SqliteLedger) -> MoneyLedger:
        self.inner = inner
        return self


class SettleDown(Partial):
    """A ledger whose `settle` fails while `down` (database gone after the provider answered)."""

    def __init__(self) -> None:
        self.down = True

    async def settle(
        self, request_id: str, charge_nano: int, attempts: tuple[AttemptRecord, ...], now: int
    ) -> Settlement:
        if self.down:
            raise LedgerUnavailable("gone")
        return await self.inner.settle(request_id, charge_nano, attempts, now)


class HoldThenFail(Partial):
    """A ledger whose `hold` commits and then loses the reply (an ambiguous COMMIT)."""

    def __init__(self) -> None:
        self.release_down = False

    async def hold(self, request: HoldRequest) -> Held | Refused:
        await self.inner.hold(request)
        raise LedgerUnavailable("commit reply lost")

    async def release(
        self, request_id: str, attempts: tuple[AttemptRecord, ...], now: int
    ) -> Settlement:
        if self.release_down:
            raise LedgerUnavailable("down")
        return await self.inner.release(request_id, attempts, now)


class GiveBackLedger(HoldThenFail):
    """`hold` commits slowly so that the caller can be cancelled while it runs."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def hold(self, request: HoldRequest) -> Held | Refused:
        await self.gate.wait()
        await self.inner.hold(request)
        raise LedgerUnavailable("commit reply lost")


async def beats(s: Stack, count: int) -> None:
    for _ in range(count):
        s.clock.advance(seconds=20)
        await s.pipeline.renew_leases()


@async_test
async def test_cr_02_heartbeat_after_a_failed_settle_lets_the_sweeper_charge_the_full_hold(
    tmp_path: Path,
) -> None:
    """CR-02: three tries with backoff, then the lease is left to lapse and the sweeper charges."""
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    ledger = SettleDown()
    async with open_stack(
        tmp_path, wrap_ledger=ledger.wrap, pipeline_kwargs={"sleep": record}
    ) as s:
        r = await s.chat()
        rid = r.headers["x-spendgate-request-id"]
        held = s.observer.held[0].hold.nano
        assert sleeps == [0.05, 0.25]  # backoff between the three tries
        assert s.pipeline.unsettled() == {rid}
        for _ in range(4):  # four 20 s heartbeats: the hold must not be kept alive
            s.clock.advance(seconds=20)
            await s.pipeline.renew_leases()
        swept = await s.ledger.sweep_expired(s.clock.now())
        assert swept.expired == (rid,)
        assert await s.totals() == (held, 0)


@async_test
async def test_cr_02_an_unsettled_close_is_retried_by_the_heartbeat(tmp_path: Path) -> None:
    """CR-02: once the database is back, the next heartbeat settles at the usage."""
    ledger = SettleDown()
    async with open_stack(
        tmp_path, wrap_ledger=ledger.wrap, pipeline_kwargs={"sleep": no_sleep}
    ) as s:
        r = await s.chat()
        rid = r.headers["x-spendgate-request-id"]
        ledger.down = False  # the database is back
        await s.pipeline.renew_leases()
        assert s.pipeline.unsettled() == frozenset()
        spent, held = await s.totals()
        assert held == 0 and spent == 3500  # settled at the usage, no sweeper involved
        res = await s.ledger.get_reservation(rid)
        assert res is not None and res.state is ReservationState.SETTLED


@async_test
async def test_cr_05_no_cost_header_when_the_ledger_did_not_book(tmp_path: Path) -> None:
    """CR-05: the cost header is only sent for a charge the ledger booked."""
    ledger = SettleDown()
    async with open_stack(
        tmp_path, wrap_ledger=ledger.wrap, pipeline_kwargs={"sleep": no_sleep}
    ) as s:
        r = await s.chat()
        assert r.status_code == 200 and "x-spendgate-cost" not in r.headers


@async_test
async def test_nf_03_a_hold_whose_commit_reply_was_lost_ends_free(tmp_path: Path) -> None:
    """NF-03: nothing was sent, so the ambiguous hold is released, never swept at the full hold."""
    async with open_stack(tmp_path, wrap_ledger=HoldThenFail().wrap) as s:
        reply = await s.chat()
        assert reply.status_code == 503
        s.assert_no_contact()
        await beats(s, 5)
        await s.ledger.sweep_expired(s.clock.now())
        assert await s.totals() == (0, 0)  # released, never swept at the full hold


@async_test
async def test_nf_03_a_failed_release_is_retried_by_the_heartbeat(tmp_path: Path) -> None:
    """NF-03: a release the ledger refused is retried by the heartbeat before the lease is up."""
    ledger = HoldThenFail()
    async with open_stack(tmp_path, wrap_ledger=ledger.wrap) as s:
        ledger.release_down = True
        assert (await s.chat()).status_code == 503
        assert len(s.pipeline.unsettled()) == 1
        ledger.release_down = False
        await s.pipeline.renew_leases()  # the retry runs before the lease is up
        await s.ledger.sweep_expired(s.clock.now() + 10 * 60 * 1000)
        assert await s.totals() == (0, 0)


@async_test
async def test_nf_03_give_back_after_a_cancelled_ambiguous_hold_ends_free(tmp_path: Path) -> None:
    """NF-03: a caller cancelled during an ambiguous hold still gets the hold given back."""
    ledger = GiveBackLedger()
    async with open_stack(tmp_path, wrap_ledger=ledger.wrap) as s:
        call = asyncio.ensure_future(s.chat())
        for _ in range(50):
            await asyncio.sleep(0)
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        ledger.gate.set()
        await s.pipeline.drain()  # the give-back runs as a background settlement
        assert await s.totals() == (0, 0)
        await beats(s, 5)
        await s.ledger.sweep_expired(s.clock.now())
        assert await s.totals() == (0, 0)
