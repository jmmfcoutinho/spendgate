"""A stream is never run unwatched (M-12, T-BLK-07; pen test H-34).

What the heartbeat re-checks for an open stream (the key's scope chain for kill switches, the
session's wall-clock limit) is read after the hold, before anything is sent. If that read fails
twice, the call ends `ledger_unavailable` before any upstream contact and its hold is released;
a single failed read is retried at once and the stream is then watched as usual.
"""

import json
from pathlib import Path

import pytest

from spendgate.core.outcomes import Outcome, ReservationState
from spendgate.core.sessions import SessionDefaults
from spendgate.ledger.port import (
    Held,
    HoldRequest,
    LedgerUnavailable,
    MoneyLedger,
    Refused,
    ScopeRow,
)
from tests.fakes.ledger import MoneyLedgerProxy
from tests.integration.harness import CHAT, async_test
from tests.integration.stream_rig import AsgiCall, chat_chunk
from tests.integration.test_gateway_stream_stops import SESSION, beat
from tests.integration.test_gateway_streaming import BODY, gated, settled_once

_DEFAULTS = SessionDefaults(max_seconds=60)


class Blinking(MoneyLedgerProxy):
    """A ledger whose scope reads fail `failures` times, for every scope or the session's only."""

    def __init__(self, inner: MoneyLedger, failures: int, *, session_only: bool) -> None:
        super().__init__(inner)
        self.failures, self.session_only = failures, session_only
        self.session: str | None = None

    async def hold(self, request: HoldRequest) -> Held | Refused:
        held = await self.inner.hold(request)
        if isinstance(held, Held):
            self.session = held.session_scope_id
        return held

    async def get_scope(self, scope_id: str) -> ScopeRow | None:
        targeted = not self.session_only or scope_id == self.session
        if targeted and self.failures > 0:
            self.failures -= 1
            raise LedgerUnavailable("database is gone")
        return await self.inner.get_scope(scope_id)


@pytest.mark.parametrize("session_only", [True, False], ids=["session-clock", "scope-chain"])
@async_test
async def test_m12_a_stream_whose_watch_facts_cannot_be_read_does_not_run_unwatched(
    tmp_path: Path, session_only: bool
) -> None:
    def wrap(ledger: MoneyLedger) -> MoneyLedger:
        return Blinking(ledger, 10**6, session_only=session_only)

    async with gated(tmp_path, wrap_ledger=wrap, session_defaults=_DEFAULTS) as (s, up):
        call = await AsgiCall(s.app, CHAT, BODY, s.secret, SESSION).start()
        body = json.loads(await call.read_all())
        await call.finish()
        assert call.status == 503 and body["error"]["code"] == "ledger_unavailable"
        assert call.headers["retry-after"] == "5"
        assert call.headers["content-type"] == "application/json"  # never an event stream
        assert up.requests == []  # zero tokens: the provider was never called
        await s.pipeline.aclose()
        rid = s.observer.held[0].request_id
        reservation = await s.ledger.get_reservation(rid)
        assert reservation is not None and reservation.state is ReservationState.RELEASED
        assert await s.totals() == (0, 0)  # released at 0: nothing held, nothing spent
        assert s.pipeline.owned() == frozenset() and s.pipeline.unsettled() == frozenset()
        settled = [(e.outcome, e.released, e.charged.nano) for e in s.observer.settled]
        assert settled == [(Outcome.INTERNAL_ERROR, True, 0)]  # nothing was sent: free (M-13)
        faults = [(e.fault.where, e.fault.error_class) for e in s.observer.internal]
        assert faults == [("pipeline.watch", "LedgerUnavailable")] * 2  # tried twice, both logged


@async_test
async def test_m12_a_transient_watch_facts_read_failure_is_retried_once(tmp_path: Path) -> None:
    def wrap(ledger: MoneyLedger) -> MoneyLedger:
        return Blinking(ledger, 1, session_only=True)

    async with gated(tmp_path, wrap_ledger=wrap, session_defaults=_DEFAULTS) as (s, up):
        call = await AsgiCall(s.app, CHAT, BODY, s.secret, SESSION).start()
        assert call.status == 200  # the second read worked: the stream runs, watched
        up.streams[0].put(chat_chunk("a"))
        assert await call.chunk() == chat_chunk("a")
        s.clock.advance(seconds=61)  # past the session's max_seconds, read on the retry
        await beat(s)
        text = (await call.read_all()).decode()
        assert "time limit" in text and '"error"' in text
        await call.finish()
        assert up.streams[0].closed
        await settled_once(s, Outcome.STREAM_BROKEN, s.observer.held[0].hold.nano)
        faults = [(e.fault.where, e.fault.error_class) for e in s.observer.internal]
        assert faults == [("pipeline.watch", "LedgerUnavailable")]  # the retried one, logged
