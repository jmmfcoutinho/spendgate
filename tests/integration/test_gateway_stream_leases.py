"""A stream outlives the default lease, so the heartbeat must keep it alive, and a stream
whose lease cannot be kept is aborted and settled (scenario 3, M-04, M-05).

The clock is the injected `FakeClock`; a heartbeat is one `Pipeline.renew_leases()` call.
"""

from pathlib import Path

from spendgate.core.outcomes import Outcome, ReservationState
from tests.integration.harness import async_test
from tests.integration.stream_rig import DONE, chat_chunk, chat_usage
from tests.integration.test_gateway_streaming import SEEN, gated, open_call, priced, settled_once

LEASE_MS = 60_000  # default lease; the heartbeat is a third of it
BEAT_S = 20


class FlakyRenewals:
    """A ledger whose `renew_all` can fail or answer with a chosen set of ids."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.fail = False
        self.answer: tuple[str, ...] | None = None  # None: ask the real ledger

    async def renew_all(self, instance: str, now: int, lease_ms: int) -> tuple[str, ...]:
        if self.fail:
            raise ConnectionError("ledger unreachable")
        if self.answer is not None:
            return self.answer
        return await self._inner.renew_all(instance, now, lease_ms)  # type: ignore[attr-defined, no-any-return]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def flaky_ledger() -> tuple[list[FlakyRenewals], object]:
    made: list[FlakyRenewals] = []

    def wrap(ledger: object) -> object:
        made.append(FlakyRenewals(ledger))
        return made[0]

    return made, wrap


@async_test
async def test_ten_minute_stream_is_kept_alive_by_the_heartbeat(tmp_path: Path) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        rid = s.observer.held[0].request_id
        for _ in range(20):  # 20 beats of 30 s: ten minutes, ten times the lease
            s.clock.advance(seconds=30)
            await s.pipeline.renew_leases()
            up.streams[0].put(chat_chunk("."))
            assert await call.chunk() == chat_chunk(".")  # still flowing: never aborted
            swept = await s.ledger.sweep_expired(s.clock.now())
            assert swept.expired == ()  # the sweeper finds nothing to charge
        reservation = await s.ledger.get_reservation(rid)
        assert reservation is not None and reservation.state is ReservationState.PENDING
        assert reservation.lease_expires_at == s.clock.now() + LEASE_MS  # renewed to the last beat
        up.streams[0].put(chat_usage(SEEN))
        up.streams[0].put(DONE)
        up.streams[0].end()
        await call.read_all()
        await call.finish()
        await settled_once(s, Outcome.OK, priced(s))  # settled at the usage, not the hold


@async_test
async def test_without_a_heartbeat_the_stream_stops_before_its_lease_runs_out(
    tmp_path: Path,
) -> None:
    """Within one heartbeat of expiry the owner aborts and settles; the sweeper never has to."""
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        s.clock.advance(seconds=LEASE_MS // 1000 - BEAT_S + 1)  # less than one beat left
        up.streams[0].put(chat_chunk("late"))
        text = (await call.read_all()).decode()
        assert '"error"' in text  # the chunk already in hand is relayed, then the stream stops
        await call.finish()
        assert up.streams[0].closed
        (row,) = await s.ledger.get_calls(s.observer.held[0].request_id)
        assert row.outcome is Outcome.STREAM_BROKEN
        await settled_once(s, Outcome.STREAM_BROKEN, s.observer.held[0].hold.nano)
        swept = await s.ledger.sweep_expired(s.clock.now() + 10 * LEASE_MS)
        assert swept.expired == ()  # nothing left for the sweeper (M-05: one transition)


@async_test
async def test_failed_renewal_aborts_an_idle_upstream_call(tmp_path: Path) -> None:
    made, wrap = flaky_ledger()
    async with gated(tmp_path, wrap_ledger=wrap) as (s, up):
        call = await open_call(s)
        up.streams[0].put(chat_chunk("a") + chat_usage(SEEN))
        assert await call.chunk() is not None
        made[0].fail = True
        await s.pipeline.renew_leases()  # the upstream is silent: only the abort can end this
        text = (await call.read_all()).decode()
        assert '"error"' in text
        await call.finish()
        assert up.streams[0].closed
        await settled_once(s, Outcome.STREAM_BROKEN, priced(s))  # usage was seen: not the hold


@async_test
async def test_a_hold_the_sweeper_took_aborts_the_stream_without_double_charging(
    tmp_path: Path,
) -> None:
    """The renewal does not return the id (a sweeper expired it): abort; late settle adds at most
    the overrun, so the money moves once (M-04, M-05)."""
    made, wrap = flaky_ledger()
    async with gated(tmp_path, wrap_ledger=wrap) as (s, up):
        call = await open_call(s)
        hold = s.observer.held[0].hold.nano
        s.clock.advance(seconds=LEASE_MS // 1000 + 1)
        swept = await s.ledger.sweep_expired(s.clock.now())
        assert swept.expired == (s.observer.held[0].request_id,)
        made[0].answer = ()  # the instance's renewal returns nothing: our id is gone
        await s.pipeline.renew_leases()
        await call.read_all()
        await call.finish()
        await s.pipeline.aclose()
        assert await s.totals() == (hold, 0)  # charged once, at the hold
        (row,) = await s.ledger.get_calls(s.observer.held[0].request_id)
        assert row.charged_nano == hold
        assert up.streams[0].closed
