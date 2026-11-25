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

Each failure is recorded under a stable `AttemptError` code, never the class name: those move
between httpx releases, and `calls.error_code` is a documented list (data-model.md).
"""

from dataclasses import dataclass

import httpx

from spendgate.core.failures import AttemptError
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
# Most specific first: the first class the exception is an instance of names it.
_CODES: tuple[tuple[type[Exception], AttemptError], ...] = (
    (httpx.ConnectError, AttemptError.CONNECT_ERROR),
    (httpx.ConnectTimeout, AttemptError.CONNECT_TIMEOUT),
    (httpx.PoolTimeout, AttemptError.POOL_TIMEOUT),
    (httpx.ProxyError, AttemptError.PROXY_ERROR),
    (httpx.UnsupportedProtocol, AttemptError.UNSUPPORTED_PROTOCOL),
    (httpx.WriteError, AttemptError.WRITE_ERROR),
    (httpx.WriteTimeout, AttemptError.WRITE_TIMEOUT),
    (httpx.ReadError, AttemptError.READ_ERROR),
    (httpx.ReadTimeout, AttemptError.READ_TIMEOUT),
    (httpx.CloseError, AttemptError.CLOSE_ERROR),
    (httpx.RemoteProtocolError, AttemptError.REMOTE_PROTOCOL_ERROR),
    (httpx.LocalProtocolError, AttemptError.LOCAL_PROTOCOL_ERROR),
    (httpx.DecodingError, AttemptError.DECODING_ERROR),
    (httpx.TooManyRedirects, AttemptError.TOO_MANY_REDIRECTS),
    (httpx.HTTPError, AttemptError.TRANSPORT_ERROR),
)


@dataclass(frozen=True, slots=True)
class SendFailure:
    sent: bool
    timeout: bool  # a timeout after send is reported as 504
    code: AttemptError  # never the exception message (it may hold a URL)


class SendError(Exception):
    """The upstream exchange failed; `failure` says whether the request left."""

    def __init__(self, failure: SendFailure) -> None:
        super().__init__(failure.code.value)
        self.failure = failure


class ResponseTooLarge(Exception):
    """The provider answered with more than `MAX_RESPONSE_BYTES`."""


def classify(exc: Exception) -> SendFailure:
    """Map any exception raised while sending to the table above (unknown means sent)."""
    sent = not isinstance(exc, _NEVER_SENT)
    return SendFailure(sent, isinstance(exc, httpx.TimeoutException), error_code(exc))


def error_code(exc: Exception) -> AttemptError:
    """The stable code of a send failure; anything unlisted is `unexpected_error`."""
    for cls, code in _CODES:
        if isinstance(exc, cls):
            return code
    return AttemptError.UNEXPECTED_ERROR


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
        raise SendError(
            SendFailure(sent=True, timeout=False, code=AttemptError.RESPONSE_TOO_LARGE)
        ) from exc
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
