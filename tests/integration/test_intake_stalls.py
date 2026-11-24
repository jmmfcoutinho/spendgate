"""Uploads that stall or vanish: they give their place back and never lock out other callers.

None of these reaches a hold, so no money moves; what is at stake is the gate staying open.
"""

import asyncio
import json
from pathlib import Path

from starlette.types import Message

from spendgate.pipeline.model import CHAT
from tests.integration.harness import async_test, open_stack
from tests.integration.uploads import (
    JSON_HEAD,
    MIB,
    Body,
    auth,
    bearer,
    read_or_refused,
    second_key,
)

HI = {"model": "alpha", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}


@async_test
async def test_sr_01_per_key_uploads_are_limited_before_the_gate_and_freed_after_the_read(
    tmp_path: Path,
) -> None:
    """SR-01: a key at its concurrency limit is refused unread, and a cancelled upload frees it."""
    async with open_stack(tmp_path, max_concurrent=1) as s:
        slow = Body([b"{"], stall=True)
        first = asyncio.ensure_future(s.pipeline.serve(CHAT, bearer(s), slow))
        await slow.stalled.wait()
        second = Body([b"{}"])
        reply = await s.pipeline.serve(CHAT, bearer(s), second)
        assert reply.status == 429 and second.pulled == 0
        assert json.loads(reply.body)["error"]["details"]["limit"] == "concurrency"
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        ok = await s.chat()  # the ticket came back, and the real limit has room
        assert ok.status_code == 200


# T-DOS-04
@async_test
async def test_sr_02_a_stalled_body_is_refused_with_408_and_frees_its_place(
    tmp_path: Path,
) -> None:
    """SR-02: a body that stops arriving ends in 408 and leaves the gate empty."""
    async with open_stack(tmp_path, pipeline_kwargs={"body_timeout_seconds": 0.05}) as s:
        reply = await s.pipeline.serve(CHAT, bearer(s), Body([b"{"], stall=True))
        assert reply.status == 408 and json.loads(reply.body)["error"]["code"] == "invalid_request"
        assert s.pipeline.gate.in_flight == 0
        s.assert_no_contact()


# T-DOS-04
@async_test
async def test_sr_02_an_unauthenticated_stalled_body_never_holds_a_place(tmp_path: Path) -> None:
    """SR-02: a caller with no key is refused before its body is read, so it cannot stall."""
    async with open_stack(tmp_path, concurrent=1) as s:
        stalled = Body([b"{"], stall=True)
        reply = await s.pipeline.serve(CHAT, JSON_HEAD, stalled)
        assert reply.status == 401 and stalled.pulled == 0 and s.pipeline.gate.in_flight == 0


@async_test
async def test_nf_01_stalled_uploads_of_one_key_do_not_lock_out_another_key(
    tmp_path: Path,
) -> None:
    """NF-01: five stalled 3 MiB uploads of one key stay inside its share; another key gets in."""
    async with open_stack(tmp_path, max_concurrent=16) as s:
        other = await second_key(s)
        stalled = [Body([b" " * 3 * MIB], stall=True) for _ in range(5)]
        calls = [
            asyncio.ensure_future(s.pipeline.serve(CHAT, auth(s.secret), body)) for body in stalled
        ]
        for call, body in zip(calls, stalled, strict=True):
            await read_or_refused(call, body)  # past its share, an upload is refused, not stalled
        assert s.pipeline.gate.buffered_bytes <= s.pipeline.gate.memory_bytes // 4
        reply = await s.post("/v1/chat/completions", HI, key=other)
        assert reply.status_code == 200
        for call in calls:
            call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)
        assert s.pipeline.gate.in_flight == 0 and s.pipeline.gate.buffered_bytes == 0


@async_test
async def test_client_leaving_before_the_body_is_read_is_not_an_unhandled_error(
    tmp_path: Path,
) -> None:
    """Load-run defect 2: `read_capped` let starlette's ClientDisconnect escape, so uvicorn logged
    'Exception in ASGI application' and a traceback for an ordinary cancelled request (before auth
    and any hold: no money effect). Seen in the load run with client-side cancels."""
    async with open_stack(tmp_path) as stack:

        async def receive() -> Message:
            return {"type": "http.disconnect"}

        async def send(_message: Message) -> None:
            return None

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
            "path": CHAT.path, "raw_path": CHAT.path.encode(), "query_string": b"",
            "root_path": "", "scheme": "http", "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 80),
            "headers": [
                (b"authorization", f"Bearer {stack.secret}".encode()),
                (b"content-type", b"application/json"),
                (b"content-length", b"100"),
            ],
        }  # fmt: skip
        await stack.app(scope, receive, send)
