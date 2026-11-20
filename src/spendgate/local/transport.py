"""httpx transports that run the request pipeline in-process.

The SDK builds an `httpx.Request`; the transport turns it into a `runtime.Call`, the pipeline
answers with a `Reply`, and the transport turns that back into an `httpx.Response`. Provider
calls leave through a real `httpx.AsyncClient` (injectable for tests).

Threading: the sync transport owns a private loop thread (see `loop.py`), so it works inside
Jupyter and in plain scripts. The async transport runs on the caller's loop and must stay on it.
Anthropic SDK 2.x needs `httpx2` objects instead; see `transport_httpx2.py`.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Literal, overload

import httpx

from spendgate.config import Settings, load
from spendgate.core.money import Money, parse_amount
from spendgate.local.bridge import (
    AsyncBridge,
    SyncBridge,
    response_headers,
)
from spendgate.local.runtime import Runtime, local_env
from spendgate.pipeline.model import ByteStream, Clock
from spendgate.pipeline.pipeline import system_ms


class _SyncStream(httpx.SyncByteStream):
    def __init__(self, bridge: SyncBridge, chunks: ByteStream) -> None:
        self._bridge, self._chunks = bridge, chunks

    def __iter__(self) -> Iterator[bytes]:
        return self._bridge.chunks(self._chunks)

    def close(self) -> None:
        self._bridge.close_stream(self._chunks)


class _AsyncStream(httpx.AsyncByteStream):
    def __init__(self, chunks: ByteStream) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        await self._chunks.aclose()


class LocalTransport(httpx.BaseTransport):
    """Synchronous transport; the pipeline runs on a private loop thread."""

    def __init__(self, runtime: Runtime) -> None:
        self._bridge = SyncBridge(runtime)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        reply = self._bridge.exchange(request)
        headers = response_headers(reply)
        if reply.stream is None:
            return httpx.Response(reply.status, headers=headers, content=reply.body)
        stream = _SyncStream(self._bridge, reply.stream)
        return httpx.Response(reply.status, headers=headers, stream=stream)

    def close(self) -> None:
        self._bridge.close()


class AsyncLocalTransport(httpx.AsyncBaseTransport):
    """Asynchronous transport; runs on the caller's loop, which it must keep using."""

    def __init__(self, runtime: Runtime) -> None:
        self._bridge = AsyncBridge(runtime)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        reply = await self._bridge.exchange(request)
        headers = response_headers(reply)
        if reply.stream is None:
            return httpx.Response(reply.status, headers=headers, content=reply.body)
        return httpx.Response(reply.status, headers=headers, stream=_AsyncStream(reply.stream))

    async def aclose(self) -> None:
        await self._bridge.close()


def make_runtime(
    settings_or_path: Settings | str | Path, ledger_path: str | Path | None, cap: str | None,
    client: httpx.AsyncClient | None, now_ms: Clock,
) -> Runtime:  # fmt: skip
    settings = (
        settings_or_path if isinstance(settings_or_path, Settings)
        else load(settings_or_path, env=local_env())
    )  # fmt: skip
    amount: Money | None = parse_amount(cap) if cap is not None else None
    path = Path(ledger_path) if ledger_path is not None else None
    return Runtime(settings, path, amount, client=client, now_ms=now_ms)


@overload
def transport(
    settings_or_path: Settings | str | Path, *, ledger_path: str | Path | None = ...,
    async_: Literal[False] = ..., cap: str | None = ..., client: httpx.AsyncClient | None = ...,
    now_ms: Clock = ...,
) -> LocalTransport: ...  # fmt: skip
@overload
def transport(
    settings_or_path: Settings | str | Path, *, ledger_path: str | Path | None = ...,
    async_: Literal[True], cap: str | None = ..., client: httpx.AsyncClient | None = ...,
    now_ms: Clock = ...,
) -> AsyncLocalTransport: ...  # fmt: skip
def transport(  # noqa: PLR0913 - the public entry point with named options
    settings_or_path: Settings | str | Path, *, ledger_path: str | Path | None = None,
    async_: bool = False, cap: str | None = None, client: httpx.AsyncClient | None = None,
    now_ms: Clock = system_ms,
) -> LocalTransport | AsyncLocalTransport:  # fmt: skip
    """An httpx transport that applies the gateway's rules in this process.

    `settings_or_path` is a loaded `Settings` or a path to a `spendgate.yaml` (its `bootstrap`
    section is not applied; scope and budget come from `cap`). `ledger_path` defaults to the
    config's SQLite path. `cap` (for example `"5 USD"`) creates the shared budget on a new ledger;
    without it the ledger must already have one, or the transport refuses to start.
    """
    runtime = make_runtime(settings_or_path, ledger_path, cap, client, now_ms)
    return AsyncLocalTransport(runtime) if async_ else LocalTransport(runtime)
