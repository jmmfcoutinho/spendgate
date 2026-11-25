"""HTTP-library-neutral half of the transports: request in, `Reply` out, lifecycle, streams.

Two httpx generations exist side by side: the OpenAI SDK uses `httpx`, the Anthropic SDK 2.x uses
`httpx2`, and each refuses the other's objects. The bridges and stream halves below know neither;
`transport.py` and `transport_httpx2.py` only put each library's base classes around them, and
`factory.py` builds the runtime either pair wraps.
"""

from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Protocol

from spendgate.client.session import current_session
from spendgate.local.loop import LoopThread
from spendgate.local.principal import session_defaults
from spendgate.local.runtime import Call, Runtime
from spendgate.pipeline.model import ByteStream, Reply


class _Url(Protocol):
    @property
    def path(self) -> str: ...


class HttpRequest(Protocol):
    """What both httpx generations' `Request` offer."""

    @property
    def method(self) -> str: ...

    @property
    def url(self) -> _Url: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    def read(self) -> bytes: ...

    async def aread(self) -> bytes: ...


def _call(request: HttpRequest, body: bytes, currency: str) -> Call:
    session = current_session()
    session_defaults(session, currency)  # a bad limit raises here, at the call site
    return Call(request.method, request.url.path, dict(request.headers), body, session)


async def _next(stream: ByteStream) -> bytes:
    return await stream.__anext__()


def response_headers(reply: Reply) -> dict[str, str]:
    """Reply headers; a live stream has no fixed length."""
    headers = dict(reply.headers)
    if reply.stream is not None:
        headers.pop("content-length", None)
    return headers


class SyncBridge:
    """Runs the pipeline on a private loop thread; the caller's thread only waits."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self._loop = LoopThread()
        self._closed = False
        try:
            self._loop.run(runtime.start())  # setup errors surface here, not on the first call
        except BaseException:
            self.close()
            raise

    def exchange(self, request: HttpRequest) -> Reply:
        call = _call(request, request.read(), self.runtime.settings.ledger_currency)
        return self._loop.run(self.runtime.handle(call))

    def chunks(self, stream: ByteStream) -> Iterator[bytes]:
        while True:
            try:
                yield self._loop.run(_next(stream))
            except StopAsyncIteration:
                return

    def close_stream(self, stream: ByteStream) -> None:
        """Always called by httpx when the response is done: it aborts upstream and settles."""
        self._loop.run(stream.aclose())

    def close(self) -> None:
        if self._closed:  # httpx closes a transport once per owner; a guard may close it too
            return
        self._closed = True
        try:
            self._loop.run(self.runtime.aclose())
        finally:
            self._loop.close()


class AsyncBridge:
    """Runs the pipeline on the caller's loop; started lazily on the first request."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    async def exchange(self, request: HttpRequest) -> Reply:
        call = _call(request, await request.aread(), self.runtime.settings.ledger_currency)
        return await self.runtime.handle(call)

    async def close(self) -> None:
        await self.runtime.aclose()


class SyncChunks:
    """A live reply body for a sync client; mixed in before the library's `SyncByteStream`."""

    def __init__(self, bridge: SyncBridge, chunks: ByteStream) -> None:
        self._bridge, self._chunks = bridge, chunks

    def __iter__(self) -> Iterator[bytes]:
        return self._bridge.chunks(self._chunks)

    def close(self) -> None:
        self._bridge.close_stream(self._chunks)


class AsyncChunks:
    """A live reply body for an async client; mixed in before the library's `AsyncByteStream`."""

    def __init__(self, chunks: ByteStream) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        await self._chunks.aclose()
