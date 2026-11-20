"""Sending one upstream request and saying whether it left.

`not_processed` needs one fact the provider cannot give: was the request ever sent? The httpx
exception tells. Connection set-up failures happen before a byte is written, so the provider
cannot have done work; anything later may have reached it and counts as sent (charged, M-13).

| httpx exception                                                   | request sent? |
|-------------------------------------------------------------------|---------------|
| ConnectError (refused, DNS, TLS handshake), ConnectTimeout        | no            |
| PoolTimeout (no connection obtained), ProxyError (tunnel failed)  | no            |
| UnsupportedProtocol (config fault, nothing opened)                | no            |
| WriteError, WriteTimeout (a partial write may have arrived)       | yes           |
| ReadError, ReadTimeout, RemoteProtocolError, LocalProtocolError   | yes           |
| any other transport or decoding error, response over the cap      | yes           |
"""

from dataclasses import dataclass

import httpx

from spendgate.pipeline.model import Sender
from spendgate.providers.port import UpstreamRequest, UpstreamResponse

# Largest non-streamed provider answer read into memory (speech audio, long completions).
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_NEVER_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
)


@dataclass(frozen=True, slots=True)
class SendFailure:
    sent: bool
    timeout: bool  # a timeout after send is reported as 504
    kind: str  # exception class name, for logs; never the message (it may hold a URL)


class SendError(Exception):
    """The upstream exchange failed; `failure` says whether the request left."""

    def __init__(self, failure: SendFailure) -> None:
        super().__init__(failure.kind)
        self.failure = failure


class ResponseTooLarge(Exception):
    """The provider answered with more than `MAX_RESPONSE_BYTES`."""


def classify(exc: Exception) -> SendFailure:
    """Map any exception raised while sending to the table above (unknown means sent)."""
    sent = not isinstance(exc, _NEVER_SENT)
    return SendFailure(sent, isinstance(exc, httpx.TimeoutException), type(exc).__name__)


def build(up: UpstreamRequest) -> httpx.Request:
    return httpx.Request(up.method, up.url, headers=dict(up.headers), content=up.body)


async def open_response(sender: Sender, up: UpstreamRequest) -> httpx.Response:
    """Send and return the response with its body unread (streaming reads it with a relay)."""
    try:
        return await sender.send(build(up), stream=True)
    except Exception as exc:
        raise SendError(classify(exc)) from exc


async def read_body(response: httpx.Response, max_bytes: int) -> UpstreamResponse:
    """Read an opened response completely and close it; `SendError` (it was sent) on failure."""
    try:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > max_bytes:
                raise ResponseTooLarge
    except ResponseTooLarge as exc:
        raise SendError(SendFailure(sent=True, timeout=False, kind="ResponseTooLarge")) from exc
    except Exception as exc:
        raise SendError(classify(exc)) from exc
    finally:
        await response.aclose()
    return UpstreamResponse(response.status_code, dict(response.headers), bytes(body))


async def exchange(
    sender: Sender, up: UpstreamRequest, max_bytes: int = MAX_RESPONSE_BYTES
) -> UpstreamResponse:
    """A complete buffered answer. Raises `SendError` for every failure, sent or not."""
    return await read_body(await open_response(sender, up), max_bytes)


def upstream_timeout(connect_seconds: int) -> httpx.Timeout:
    """Read/total 600 s (calls are capped elsewhere); the connect timeout is configurable."""
    return httpx.Timeout(600.0, connect=float(connect_seconds))
