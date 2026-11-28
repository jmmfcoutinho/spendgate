"""A scriptable fake provider: one Starlette ASGI app that speaks every provider's wire shape.

Tests drive it through `FakeUpstream.client()`: an `httpx.AsyncClient` on an ASGI transport, so no
socket is opened (the autouse socket guard stays on). Connection-level failures that an ASGI app
cannot express are marked with a header by the app and turned into real httpx errors by
`FaultTransport`, so the code under test sees the exceptions a real network would raise.
"""

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from tests.fakes.script import Script
from tests.fakes.shape_types import JSON, Ctx, Reply, Shape
from tests.fakes.shapes import shape_for

FAULT_HEADER = "x-fake-fault"  # app -> transport signal; stripped before the client sees it
BASE_URL = "https://upstream.test"

type Delay = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """One request that reached the fake, as the upstream saw it."""

    method: str
    path: str
    query: Mapping[str, str]
    headers: Mapping[str, str]
    body: bytes
    shape: str | None

    def json(self) -> Mapping[str, object]:
        data = json.loads(self.body)
        assert isinstance(data, dict)
        return data


class FakeUpstream:
    """The app, its request log and its scripts. One instance per test."""

    def __init__(self, default: Script | None = None, delay: Delay | None = None) -> None:
        self.default = default or Script()
        self.requests: list[RecordedRequest] = []
        self.delays: list[float] = []  # seconds each injected delay asked for
        self.cancelled = 0  # requests whose client went away while the fake was holding them
        self._delay = delay or self._record_delay
        self._queue: deque[Script] = deque()
        self.app = Starlette(routes=[Route("/{path:path}", self._handle, methods=["GET", "POST"])])

    # --- scripting -------------------------------------------------------------------------

    def enqueue(self, *scripts: Script) -> None:
        """Scripts for the next requests, in order; then `default` applies again."""
        self._queue.extend(scripts)

    def peek(self) -> Script:
        return self._queue[0] if self._queue else self.default

    def take(self) -> Script:
        return self._queue.popleft() if self._queue else self.default

    async def _record_delay(self, seconds: float) -> None:
        # Latency is data, not time: tests assert on `delays` and nothing really sleeps.
        self.delays.append(seconds)

    # --- the request log -------------------------------------------------------------------

    @property
    def calls(self) -> int:
        return len(self.requests)

    def assert_no_contact(self) -> None:
        assert not self.requests, f"upstream was contacted {self.calls} time(s)"

    async def wait_for_calls(self, n: int = 1) -> None:
        """Yield to the loop until `n` requests arrived (a bare `sleep(0)` is not a real sleep)."""
        for _ in range(1000):
            if self.calls >= n:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"fewer than {n} request(s) reached the fake")

    # --- httpx plumbing --------------------------------------------------------------------

    def transport(self) -> "FaultTransport":
        return FaultTransport(self)

    def client(self, **kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport(), base_url=BASE_URL, **kwargs)  # type: ignore[arg-type]

    # --- the app ---------------------------------------------------------------------------

    async def _handle(self, request: Request) -> Response:
        script = self.take()
        raw = await request.body()
        shape = shape_for(request.url.path)
        self.requests.append(
            RecordedRequest(
                request.method,
                request.url.path,
                dict(request.query_params),
                dict(request.headers),
                raw,
                shape.name if shape else None,
            )
        )
        if script.disconnect_before_body:
            return Response(status_code=200, headers={FAULT_HEADER: "disconnect"})
        if script.hang:
            await self._hold_forever()
        if script.latency_ms:
            await self._delay(script.latency_ms / 1000)
        if shape is None:
            return Response(b'{"error":"unknown path"}', 404, media_type=JSON)
        ctx = Ctx(script, _parse_json(raw), request.url.path, dict(request.query_params))
        return await self._answer(shape, ctx)

    async def _hold_forever(self) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise

    async def _answer(self, shape: Shape, ctx: Ctx) -> Response:
        script = ctx.script
        extra = dict(script.headers)
        if script.retry_after is not None:
            extra["Retry-After"] = script.retry_after
        if script.body is not None:
            return Response(script.body, script.status, extra, JSON)
        if script.status >= 400:
            return _plain(shape.error(script.status, "scripted failure"), script.status, extra)
        if not shape.streaming(ctx):
            return _plain(shape.reply(ctx), 200, extra)
        content_type, frames = shape.stream(ctx)
        if script.cut_after_frames is not None:
            frames = frames[: script.cut_after_frames]
            extra[FAULT_HEADER] = "cut"
        return StreamingResponse(self._emit(frames, script), 200, extra, content_type)

    async def _emit(self, frames: list[bytes], script: Script) -> AsyncIterator[bytes]:
        for frame in frames:
            if script.frame_latency_ms:
                await self._delay(script.frame_latency_ms / 1000)
            yield frame


def _parse_json(raw: bytes) -> Mapping[str, object]:
    try:
        data = json.loads(raw)
    except ValueError:  # multipart or empty body
        return {}
    return data if isinstance(data, dict) else {}


def _plain(reply: Reply, status: int, extra: Mapping[str, str]) -> Response:
    headers = {**dict(reply.headers), **extra}
    return Response(reply.body, status, headers, reply.content_type)


class _CutStream(httpx.AsyncByteStream):
    """Delivers the bytes that were sent, then fails like a connection dropped mid-body."""

    def __init__(self, parts: list[bytes]) -> None:
        self._parts = parts

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for part in self._parts:
            yield part
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message")


class FaultTransport(httpx.AsyncBaseTransport):
    """ASGI transport plus the failures a socket would show: refused, dropped, cut mid-body."""

    def __init__(self, fake: FakeUpstream) -> None:
        self._fake = fake
        self._inner = httpx.ASGITransport(app=fake.app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._fake.peek().refuse_connection:
            self._fake.take()  # consume the script; the request is never sent or logged
            raise httpx.ConnectError("connection refused (fake)", request=request)
        response = await self._inner.handle_async_request(request)
        fault = response.headers.get(FAULT_HEADER)
        if fault == "disconnect":
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response.", request=request
            )
        if fault != "cut":
            return response
        parts = [part async for part in response.stream]  # type: ignore[union-attr]
        headers = httpx.Headers(response.headers)
        del headers[FAULT_HEADER]
        return httpx.Response(
            response.status_code, headers=headers, stream=_CutStream(parts), request=request
        )


class RefusingTransport(httpx.AsyncBaseTransport):
    """Wrap any transport; the first `refusals` requests fail with ConnectError before sending."""

    def __init__(self, inner: httpx.AsyncBaseTransport, refusals: int = 1) -> None:
        self._inner = inner
        self.remaining = refusals
        self.refused = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.remaining > 0:
            self.remaining -= 1
            self.refused += 1
            raise httpx.ConnectError("connection refused (fake)", request=request)
        return await self._inner.handle_async_request(request)
