"""CR-03: library mode keeps a long stream alive through the pipeline's heartbeat.

The maintenance loop used to renew the ledger directly, so the pipeline's own lease bookkeeping
never moved and every stream was cut once one lease (60 s by default) had passed.
"""

import asyncio
import dataclasses
import json
import logging
from pathlib import Path

import httpx
import pytest

from spendgate.core.money import Money
from spendgate.local.runtime import Call, Runtime
from tests.conftest import FakeClock
from tests.fakes import FakeUsage
from tests.integration.harness import NOW_MS, async_test
from tests.integration.stream_rig import DONE, GatedUpstream, chat_chunk, chat_usage
from tests.integration.test_local_support import CHAT, settings_in

BODY = {
    "model": "alpha",
    "max_tokens": 100,
    "stream": True,
    "messages": [{"role": "user", "content": "hi"}],
}


@async_test
async def test_t_lib_01_a_library_stream_longer_than_one_lease_is_not_cut(tmp_path: Path) -> None:
    clock, upstream = FakeClock(NOW_MS), GatedUpstream()
    client = httpx.AsyncClient(transport=upstream)
    settings = settings_in(tmp_path)
    runtime = Runtime(
        settings, tmp_path / "local.db", Money.from_amount("10", "USD"),
        client=client, now_ms=clock.now,
    )  # fmt: skip
    pipeline = await runtime.start()
    runtime._beat and runtime._beat.cancel()  # replaced below by a loop whose sleep the test drives
    at_sleep, release = asyncio.Event(), asyncio.Queue[None]()

    async def tick(_seconds: float) -> None:
        at_sleep.set()
        await release.get()

    maintenance = asyncio.ensure_future(runtime._maintain(pipeline, sleep=tick))

    async def beat() -> None:
        """Advance the clock one heartbeat and let one maintenance round run to its next sleep."""
        clock.advance(ms=settings.heartbeat_ms)
        at_sleep.clear()
        release.put_nowait(None)
        await asyncio.wait_for(at_sleep.wait(), 5)

    try:
        reply = await runtime.handle(
            Call(
                "POST", CHAT, {"content-type": "application/json"}, json.dumps(BODY).encode(), None
            )
        )
        assert reply.stream is not None
        frames = aiter(reply.stream)
        await asyncio.sleep(0)  # let the upstream request arrive
        while not upstream.streams:
            await asyncio.sleep(0)
        await beat()
        total = 0
        while total < 3 * settings.lease_ms:  # three leases long
            await beat()
            total += settings.heartbeat_ms
            upstream.streams[0].put(chat_chunk("."))
            assert chat_chunk(".") in await asyncio.wait_for(anext(frames), 5)
        upstream.streams[0].put(chat_usage(FakeUsage(input_tokens=10, output_tokens=5)))
        upstream.streams[0].put(DONE)
        upstream.streams[0].end()
        rest = b"".join([f async for f in frames])
        assert b"upstream_unavailable" not in rest and rest.endswith(DONE)
    finally:
        maintenance.cancel()
        await asyncio.gather(maintenance, return_exceptions=True)
        await runtime.aclose()
        await client.aclose()


@async_test
async def test_t_lib_02_library_mode_logs_fail_open_options_at_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """CR-09: `guard(config=...)` with a fail-open option on must not start silently."""
    base = settings_in(tmp_path)
    opened = base.config.fail_open.model_copy(update={"allow_remote_media": True})
    settings = dataclasses.replace(
        base, config=base.config.model_copy(update={"fail_open": opened})
    )
    client = httpx.AsyncClient(transport=GatedUpstream())
    runtime = Runtime(
        settings,
        tmp_path / "local.db",
        Money.from_amount("1", "USD"),
        client=client,
        now_ms=FakeClock().now,
    )
    with caplog.at_level(logging.INFO, logger="spendgate.local"):
        await runtime.start()
    await runtime.aclose()
    await client.aclose()
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("allow_remote_media is ON" in r.getMessage() for r in warned)
