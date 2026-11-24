"""`server.max_request_seconds`: a call or stream that runs too long is ended and charged (M-13).

A buffered call past the deadline is a 504 at the full hold. A stream past it is broken at its
next chunk, charged its usage if any was seen and the full hold if not. A stream nobody reads is
ended by the heartbeat instead (test_unread_streams.py).
"""

from pathlib import Path

from spendgate.core.outcomes import Outcome
from tests.fakes import Script
from tests.integration.harness import async_test, open_stack
from tests.integration.stream_rig import chat_chunk
from tests.integration.test_gateway_streaming import BODY as STREAM_BODY
from tests.integration.test_gateway_streaming import gated, open_call, settled_once


@async_test
async def test_sr_13_a_call_that_runs_too_long_is_timed_out_at_the_full_hold(
    tmp_path: Path,
) -> None:
    """SR-13: a provider that hangs past the deadline is a 504, charged the full hold."""
    async with open_stack(tmp_path, pipeline_kwargs={"max_request_seconds": 0.05}) as s:
        s.fake.enqueue(Script(hang=True))
        r = await s.chat()
        assert r.status_code == 504 and r.json()["error"]["code"] == "upstream_unavailable"
        held = s.observer.held[0]
        assert await s.totals() == (held.hold.nano, 0)
        (row,) = await s.ledger.get_calls(held.request_id)
        assert row.outcome is Outcome.TIMEOUT


@async_test
async def test_sr_13_a_stream_that_runs_too_long_is_broken_with_usage_if_seen_else_the_hold(
    tmp_path: Path,
) -> None:
    """SR-13: the next chunk after the deadline breaks the stream; no usage seen, the full hold."""
    async with gated(tmp_path, pipeline_kwargs={"max_request_seconds": 120}) as (s, up):
        call = await open_call(s, STREAM_BODY)
        for _ in range(4):  # 30 s beats keep the lease alive; the stream is at 120 s
            s.clock.advance(seconds=30)
            await s.pipeline.renew_leases()
        up.streams[0].put(chat_chunk("late"))
        text = (await call.read_all()).decode()
        assert '"error"' in text
        await call.finish()
        held = s.observer.held[0]
        (row,) = await s.ledger.get_calls(held.request_id)
        assert row.outcome is Outcome.STREAM_BROKEN
        assert "longer than the gateway allows" in text
        await settled_once(s, Outcome.STREAM_BROKEN, held.hold.nano)
