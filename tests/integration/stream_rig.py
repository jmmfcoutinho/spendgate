"""Test rig for live streams: a client that reads the gateway chunk by chunk (httpx's ASGI transport
buffers a whole response, so it cannot show streaming or a disconnect) and an upstream that lets
the test release its frames one at a time.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping

import httpx
from starlette.applications import Starlette

from tests.fakes import FakeUsage

_WAIT = 5.0  # only turns a hang into a failure


class AsgiCall:
    """One POST to the gateway app, driven by hand: read chunks as they are sent, then leave."""

    def __init__(
        self, app: Starlette, path: str, body: Mapping[str, object], key: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:  # fmt: skip
        self._app, self._path, self._key = app, path, key
        self._extra = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        self._raw = json.dumps(body).encode()
        self._sent: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self._gone = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.status = 0
        self.headers: dict[str, str] = {}
        self.body = b""

    async def start(self) -> "AsgiCall":
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "path": self._path, "raw_path": b"",
            "query_string": b"", "scheme": "http", "server": ("gateway.test", 80),
            "client": ("127.0.0.1", 1), "root_path": "",
            "headers": [
                (b"host", b"gateway.test"), (b"content-type", b"application/json"),
                (b"authorization", f"Bearer {self._key}".encode()),
                (b"content-length", str(len(self._raw)).encode()),
                *self._extra,
            ],
        }  # fmt: skip
        sent_body = False

        async def receive() -> dict[str, object]:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": self._raw, "more_body": False}
            await self._gone.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            await self._sent.put(message)

        self._task = asyncio.ensure_future(self._app(scope, receive, send))  # type: ignore[arg-type]
        head = await self._message()
        self.status = int(head["status"])  # type: ignore[call-overload]
        raw_headers: list[tuple[bytes, bytes]] = head["headers"]  # type: ignore[assignment]
        self.headers = {k.decode(): v.decode() for k, v in raw_headers}
        return self

    async def _message(self) -> dict[str, object]:
        return await asyncio.wait_for(self._sent.get(), _WAIT)

    async def chunk(self) -> bytes | None:
        """The next body chunk the gateway sent; None once the response is complete."""
        message = await self._message()
        data: bytes = message.get("body", b"")  # type: ignore[assignment]
        self.body += data
        return data if message.get("more_body") else None

    async def read_all(self) -> bytes:
        while await self.chunk() is not None:
            pass
        return self.body

    async def disconnect(self) -> None:
        """The client goes away; waits until the gateway's handler has fully returned."""
        self._gone.set()
        assert self._task is not None
        await asyncio.wait_for(asyncio.gather(self._task, return_exceptions=True), _WAIT)

    async def finish(self) -> None:
        assert self._task is not None
        await asyncio.wait_for(self._task, _WAIT)


class GatedStream(httpx.AsyncByteStream):
    """An upstream body the test feeds: `put` a frame, `end()` it, or `cut()` the connection."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes | Exception | None] = asyncio.Queue()
        self.closed = False  # the gateway dropped the upstream connection

    def put(self, frame: bytes) -> None:
        self._queue.put_nowait(frame)

    def end(self) -> None:
        self._queue.put_nowait(None)

    def cut(self) -> None:
        self._queue.put_nowait(httpx.RemoteProtocolError("peer closed connection mid-body"))

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def aclose(self) -> None:
        self.closed = True


class GatedUpstream(httpx.AsyncBaseTransport):
    """Answers every request with a 200 event stream whose frames the test controls."""

    def __init__(self) -> None:
        self.streams: list[GatedStream] = []
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        stream = GatedStream()
        self.streams.append(stream)
        headers = {"content-type": "text/event-stream"}
        return httpx.Response(200, headers=headers, stream=stream, request=request)


def sse(payload: Mapping[str, object], event: str | None = None) -> bytes:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(payload)}\n\n".encode()


def chat_chunk(text: str) -> bytes:
    delta = {"index": 0, "delta": {"content": text}, "finish_reason": None}
    return sse({"id": "c", "object": "chat.completion.chunk", "choices": [delta]})


def chat_usage(usage: FakeUsage | None = None) -> bytes:
    """The final usage-only chunk the gateway forces upstream (and strips for the client)."""
    u = usage or FakeUsage()
    total = u.input_tokens + u.output_tokens
    counts = {
        "prompt_tokens": u.input_tokens, "completion_tokens": u.output_tokens,
        "total_tokens": total,
    }  # fmt: skip
    return sse({"id": "c", "object": "chat.completion.chunk", "choices": [], "usage": counts})


DONE = b"data: [DONE]\n\n"
