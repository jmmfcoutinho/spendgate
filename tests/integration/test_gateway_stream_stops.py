"""Protocol 2.8: a kill switch engaged mid-stream, or a session's wall-clock limit, stops a live
stream. It settles like any other abort: the usage seen so far, else the full hold (M-13).

The clock is the injected `FakeClock`; a heartbeat is `renew_leases()` then `watch_streams()`.
"""

from pathlib import Path

import pytest

from spendgate.core.outcomes import Outcome
from spendgate.core.policy import KillSwitch, KillTarget
from spendgate.core.sessions import SessionDefaults
from tests.integration.harness import CHAT, NOW_MS, Stack, async_test
from tests.integration.stream_rig import AsgiCall, chat_chunk, chat_usage
from tests.integration.test_gateway_streaming import (
    BODY,
    SEEN,
    gated,
    open_call,
    priced,
    settled_once,
)

SESSION = {"X-Spendgate-Session": "run-1"}


async def beat(s: Stack) -> None:
    await s.pipeline.renew_leases()  # keeps the lease out of the picture
    await s.pipeline.watch_streams()


async def engage(s: Stack, kind: KillTarget, ref: str) -> None:
    await s.ledger.engage_kill_switch(KillSwitch(kind, ref), actor="t", reason="test", now=NOW_MS)


async def target_of(s: Stack, kind: KillTarget) -> str:
    key = (await s.ledger.list_keys(s.scope.id))[0]
    return {
        KillTarget.GLOBAL: "", KillTarget.SCOPE: s.scope.id, KillTarget.KEY: key.id,
        KillTarget.MODEL: "openai/gpt-6-luna", KillTarget.PROVIDER: "openai",
    }[kind]  # fmt: skip


@pytest.mark.parametrize("kind", list(KillTarget))
@async_test
async def test_a_kill_switch_engaged_mid_stream_stops_it_and_charges_the_full_hold(
    tmp_path: Path, kind: KillTarget
) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        up.streams[0].put(chat_chunk("a"))
        assert await call.chunk() == chat_chunk("a")
        await engage(s, kind, await target_of(s, kind))
        await beat(s)  # no request traffic: only the heartbeat notices
        text = (await call.read_all()).decode()
        assert "kill switch" in text and '"error"' in text
        await call.finish()
        assert up.streams[0].closed
        await settled_once(s, Outcome.STREAM_BROKEN, s.observer.held[0].hold.nano)


@async_test
async def test_m18_a_switch_on_the_key_a_stream_key_replaced_stops_the_stream(
    tmp_path: Path,
) -> None:
    async with gated(tmp_path) as (s, up):
        old = (await s.ledger.list_keys(s.scope.id))[0]
        fresh = await s.keys.rotate_key(old.id, grace_ms=60_000, actor="t", now=NOW_MS)
        call = await AsgiCall(s.app, CHAT, BODY, fresh.secret).start()
        up.streams[0].put(chat_chunk("a"))
        assert await call.chunk() == chat_chunk("a")
        await engage(s, KillTarget.KEY, old.id)  # the old id, after the rotation
        await beat(s)
        assert "kill switch" in (await call.read_all()).decode()
        await call.finish()
        await settled_once(s, Outcome.STREAM_BROKEN, s.observer.held[0].hold.nano)


@async_test
async def test_a_kill_switch_stop_settles_at_the_usage_already_seen(tmp_path: Path) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        up.streams[0].put(chat_chunk("a") + chat_usage(SEEN))
        assert await call.chunk() is not None
        await engage(s, KillTarget.MODEL, "openai/gpt-6-luna")
        await beat(s)
        await call.read_all()
        await call.finish()
        await settled_once(s, Outcome.STREAM_BROKEN, priced(s))


@async_test
async def test_a_kill_switch_on_something_else_leaves_the_stream_running(tmp_path: Path) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        await engage(s, KillTarget.MODEL, "anthropic/other")
        await beat(s)
        up.streams[0].put(chat_chunk("still"))
        assert await call.chunk() == chat_chunk("still")
        up.streams[0].put(chat_usage(SEEN))
        up.streams[0].end()
        await call.read_all()
        await call.finish()
        await settled_once(s, Outcome.OK, priced(s))


@async_test
async def test_a_session_whose_wall_clock_limit_passes_mid_stream_is_stopped(
    tmp_path: Path,
) -> None:
    defaults = SessionDefaults(max_seconds=60)
    async with gated(tmp_path, session_defaults=defaults) as (s, up):
        call = await AsgiCall(s.app, CHAT, BODY, s.secret, SESSION).start()
        up.streams[0].put(chat_chunk("a"))
        assert await call.chunk() == chat_chunk("a")
        s.clock.advance(seconds=59)
        await beat(s)
        up.streams[0].put(chat_chunk("b"))
        assert await call.chunk() == chat_chunk("b")  # inside the limit: untouched
        s.clock.advance(seconds=2)  # 61 s since the session's first call
        await beat(s)
        text = (await call.read_all()).decode()
        assert "time limit" in text and '"error"' in text
        await call.finish()
        assert up.streams[0].closed
        await settled_once(s, Outcome.STREAM_BROKEN, s.observer.held[0].hold.nano)


@async_test
async def test_a_stream_without_a_session_clock_is_not_stopped_by_time(tmp_path: Path) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        s.clock.advance(seconds=30)
        await beat(s)
        up.streams[0].put(chat_chunk("a"))
        assert await call.chunk() == chat_chunk("a")
        up.streams[0].put(chat_usage(SEEN))
        up.streams[0].end()
        await call.read_all()
        await call.finish()
        await settled_once(s, Outcome.OK, priced(s))


@async_test
async def test_the_heartbeat_loop_itself_checks_open_streams(tmp_path: Path) -> None:
    async with gated(tmp_path) as (s, _up):
        call = await open_call(s)
        await engage(s, KillTarget.GLOBAL, "")
        ticks = 0

        async def sleep(_seconds: float) -> None:
            nonlocal ticks
            ticks += 1
            if ticks > 1:
                raise KeyboardInterrupt  # ends the loop after one full beat

        with pytest.raises(KeyboardInterrupt):
            await s.pipeline.heartbeat(sleep)
        text = (await call.read_all()).decode()
        assert '"error"' in text
        await call.finish()
        await settled_once(s, Outcome.STREAM_BROKEN, s.observer.held[0].hold.nano)
