"""Streams the client stopped reading, or whose close raised: still ended and settled once.

The heartbeat's stream watch enforces the deadline and kill switches on a stream nobody iterates,
and a close that raises still settles once and leaves the owned set, so it is no longer renewed.
"""

import json
from pathlib import Path

import pytest

from spendgate.core.outcomes import Outcome, ReservationState
from spendgate.core.policy import KillSwitch, KillTarget
from spendgate.pipeline.model import CHAT, ByteStream
from tests.integration.harness import Stack, async_test
from tests.integration.test_gateway_streaming import BODY as STREAM_BODY
from tests.integration.test_gateway_streaming import gated
from tests.integration.uploads import Body, bearer


async def open_stream(s: Stack) -> ByteStream:
    """Start a streamed call through `serve` and keep its body without ever iterating it."""
    raw = json.dumps(STREAM_BODY).encode()
    reply = await s.pipeline.serve(CHAT, bearer(s), Body([raw]))
    assert reply.status == 200 and reply.stream is not None
    return reply.stream


@async_test
async def test_nf_02_a_stream_nobody_reads_is_stopped_at_its_deadline_and_settled(
    tmp_path: Path,
) -> None:
    """NF-02: the stream watch enforces `max_request_seconds` on a stream nobody iterates."""
    async with gated(tmp_path, pipeline_kwargs={"max_request_seconds": 120}) as (s, _up):
        body = await open_stream(s)
        rid = s.observer.held[0].request_id
        for _ in range(8):  # 20 s heartbeats, 160 s: the stream is never iterated
            s.clock.advance(seconds=20)
            await s.pipeline.renew_leases()
            await s.pipeline.watch_streams()
        await s.pipeline.drain()  # the abort settles as a background task
        res = await s.ledger.get_reservation(rid)
        assert res is not None and res.state is ReservationState.SETTLED
        held = s.observer.held[0].hold.nano
        assert await s.totals() == (held, 0)  # no usage seen: the full hold
        (row,) = await s.ledger.get_calls(rid)
        assert row.outcome is Outcome.STREAM_BROKEN
        assert rid not in s.pipeline.owned()
        await body.aclose()


@async_test
async def test_nf_02_a_kill_switch_settles_a_stream_nobody_reads(tmp_path: Path) -> None:
    """NF-02: a kill switch stops and settles a stream nobody iterates, at the next heartbeat."""
    async with gated(tmp_path) as (s, _up):
        body = await open_stream(s)
        rid = s.observer.held[0].request_id
        await s.ledger.engage_kill_switch(
            KillSwitch(KillTarget.GLOBAL), actor="t", reason="test", now=s.clock.now()
        )
        await s.pipeline.renew_leases()
        await s.pipeline.watch_streams()
        await s.pipeline.drain()  # the abort settles as a background task
        (row,) = await s.ledger.get_calls(rid)
        assert row.outcome is Outcome.STREAM_BROKEN
        await body.aclose()


@async_test
async def test_nf_04_a_stream_close_that_raises_still_settles_and_is_no_longer_renewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NF-04: a close whose usage reader raises settles at the full hold and leaves `owned()`."""

    def boom(*args: object, **kwargs: object) -> object:
        raise TypeError("injected")

    async with gated(tmp_path) as (s, _up):
        body = await open_stream(s)
        rid = s.observer.held[0].request_id
        monkeypatch.setattr("spendgate.pipeline.streaming.read_usage", boom)
        with pytest.raises(TypeError):
            await body.aclose()
        res = await s.ledger.get_reservation(rid)
        assert res is not None and res.state is ReservationState.SETTLED
        assert rid not in s.pipeline.owned()
        assert await s.totals() == (s.observer.held[0].hold.nano, 0)  # a flight was open: full hold
