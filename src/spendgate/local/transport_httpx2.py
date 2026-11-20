"""The same transports for `httpx2`, which the Anthropic SDK 2.x requires (it rejects `httpx`).

Imported only by `guard().anthropic()` or by callers who build an `httpx2` client themselves, so
a base install without the `anthropic` extra never loads it.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Literal, overload

import httpx
import httpx2

from spendgate.config import Settings
from spendgate.local.bridge import (
    AsyncBridge,
    SyncBridge,
    response_headers,
)
from spendgate.local.transport import make_runtime
from spendgate.pipeline.model import ByteStream, Clock
from spendgate.pipeline.pipeline import system_ms


class _SyncStream(httpx2.SyncByteStream):
    def __init__(self, bridge: SyncBridge, chunks: ByteStream) -> None:
        self._bridge, self._chunks = bridge, chunks

    def __iter__(self) -> Iterator[bytes]:
        return self._bridge.chunks(self._chunks)

    def close(self) -> None:
        self._bridge.close_stream(self._chunks)


class _AsyncStream(httpx2.AsyncByteStream):
    def __init__(self, chunks: ByteStream) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        await self._chunks.aclose()


class LocalTransport2(httpx2.BaseTransport):
    def __init__(self, bridge: SyncBridge) -> None:
        self._bridge = bridge

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        reply = self._bridge.exchange(request)
        headers = response_headers(reply)
        if reply.stream is None:
            return httpx2.Response(reply.status, headers=headers, content=reply.body)
        stream = _SyncStream(self._bridge, reply.stream)
        return httpx2.Response(reply.status, headers=headers, stream=stream)

    def close(self) -> None:
        self._bridge.close()


class AsyncLocalTransport2(httpx2.AsyncBaseTransport):
    def __init__(self, bridge: AsyncBridge) -> None:
        self._bridge = bridge

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        reply = await self._bridge.exchange(request)
        headers = response_headers(reply)
        if reply.stream is None:
            return httpx2.Response(reply.status, headers=headers, content=reply.body)
        return httpx2.Response(reply.status, headers=headers, stream=_AsyncStream(reply.stream))

    async def aclose(self) -> None:
        await self._bridge.close()


@overload
def transport2(
    settings_or_path: Settings | str | Path, *, ledger_path: str | Path | None = ...,
    async_: Literal[False] = ..., cap: str | None = ..., client: httpx.AsyncClient | None = ...,
    now_ms: Clock = ...,
) -> LocalTransport2: ...  # fmt: skip
@overload
def transport2(
    settings_or_path: Settings | str | Path, *, ledger_path: str | Path | None = ...,
    async_: Literal[True], cap: str | None = ..., client: httpx.AsyncClient | None = ...,
    now_ms: Clock = ...,
) -> AsyncLocalTransport2: ...  # fmt: skip
def transport2(  # noqa: PLR0913 - mirrors `transport`
    settings_or_path: Settings | str | Path, *, ledger_path: str | Path | None = None,
    async_: bool = False, cap: str | None = None, client: httpx.AsyncClient | None = None,
    now_ms: Clock = system_ms,
) -> LocalTransport2 | AsyncLocalTransport2:  # fmt: skip
    """`transport()` for `httpx2` clients; provider calls still use an `httpx.AsyncClient`."""
    runtime = make_runtime(settings_or_path, ledger_path, cap, client, now_ms)
    if async_:
        return AsyncLocalTransport2(AsyncBridge(runtime))
    return LocalTransport2(SyncBridge(runtime))
