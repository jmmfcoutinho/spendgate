"""Step 1: size and concurrency limits, taken before any body byte is read.

`Gate.enter` is non-blocking: when the process is full the request is refused at once instead of
queueing. It bounds two things: requests in flight, and the body bytes those requests may hold in
memory (the audio path keeps a few copies of an upload alive, `COPIES`).

Gate memory is charged by the bytes actually buffered, never by a worst case taken up front: a
declared Content-Length is charged at once (`length * copies`), a missing one starts at zero, and
the read reserves `buffered * copies` as each chunk arrives (`Slot.grow`), refusing with 429 when
the gate cannot take more (NF-01). One key may hold at most `KEY_SHARE` of the budget, except that
it can always have one request in flight, so a single key cannot starve the others. The budget is
`max(DEFAULT_MEMORY_BYTES, max_body_bytes * COPIES, max_json_body_bytes * JSON_AMPLIFICATION)`;
raising `max_json_body_bytes` above about 12 MiB pushes it past the 512 MiB reference container
and needs a bigger one.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass

from spendgate.core.errors import ErrorCode, LimitName, Refusal
from spendgate.pipeline.model import Endpoint, Inbound
from spendgate.pipeline.multipart import parse_multipart
from spendgate.providers.port import Json

# Copies of one upload alive at once: the read buffer, the extracted file part, and the body the
# adapter rebuilds for the provider. Measured in tests/integration (see its docstring).
COPIES = 3
# A JSON body becomes Python objects: `[{},{},...]` measured 24x its size (SR-01), plus the raw
# bytes. This is what the gate charges per JSON byte, so a burst of them stays under the budget.
JSON_AMPLIFICATION = 32
# The in-flight body memory the gate allows by default: under the reference container's 512 MiB
# limit with room for the interpreter and the ledger (never below one maximal upload).
DEFAULT_MEMORY_BYTES = 384 * 1024 * 1024
_RETRY_HINT_SECONDS = 1
# Minimum progress while a body is read: this many bytes in every window of this many seconds.
PROGRESS_WINDOW_SECONDS = 5.0
PROGRESS_MIN_BYTES = 1024
# The share of the gate's memory one key may hold across its in-flight requests (NF-01).
KEY_SHARE = 4  # a quarter


def too_large(limit: int) -> Refusal:
    return Refusal(
        ErrorCode.REQUEST_TOO_LARGE,
        f"The request body is larger than the limit of {limit} bytes.",
        details={"max_body_bytes": limit},
    )


@dataclass(slots=True)
class Slot:
    """A taken place in the gate; `release` is idempotent, `grow` reserves more memory."""

    _gate: "Gate"
    _owner: str
    _copies: int
    _weight: int = 0
    _open: bool = True

    def grow(self, buffered: int) -> Refusal | None:
        """Make sure `buffered` body bytes (times the copies alive) are reserved, or refuse."""
        extra = buffered * self._copies - self._weight
        if extra <= 0 or not self._open:
            return None
        refusal = self._gate._take(self._owner, self._weight, extra)
        if refusal is None:
            self._weight += extra
        return refusal

    def release(self) -> None:
        if self._open:
            self._open = False
            self._gate._leave(self._owner, self._weight)


class Gate:
    """Concurrency and in-flight body-memory limits for one process."""

    def __init__(self, max_requests: int, max_body_bytes: int, memory_bytes: int) -> None:
        if min(max_requests, max_body_bytes, memory_bytes) < 1:
            raise ValueError("gate limits must be positive")
        self._max_requests = max_requests
        self._max_body = max_body_bytes
        self._memory = memory_bytes
        self._requests = 0
        self._bytes = 0
        self._by_owner: dict[str, int] = {}

    @property
    def in_flight(self) -> int:
        return self._requests

    @property
    def memory_bytes(self) -> int:
        return self._memory

    @property
    def buffered_bytes(self) -> int:
        """Body bytes charged now (each times its copies): what `memory_bytes` bounds."""
        return self._bytes

    def enter(
        self,
        content_length: int | None,
        *,
        max_body: int | None = None,
        copies: int = COPIES,
        owner: str = "",
    ) -> Slot | Refusal:
        """Take a place or refuse; a declared length is charged now, an absent one as it arrives.

        `max_body` is this request's own cap (JSON has a smaller one than an upload), `copies`
        how many times the body is alive in memory at once and `owner` the key it counts against.
        """
        cap = min(max_body or self._max_body, self._max_body)
        if content_length is not None and content_length > cap:
            return too_large(cap)
        weight = (content_length or 0) * copies
        if self._requests >= self._max_requests:
            return _FULL
        refusal = self._take(owner, 0, weight)
        if refusal is not None:
            return refusal
        self._requests += 1
        return Slot(self, owner, copies, weight)

    def _take(self, owner: str, own: int, extra: int) -> Refusal | None:
        """Reserve `extra` bytes for `owner` (who already holds `own` in this request)."""
        held = self._by_owner.get(owner, 0)
        over_share = held > own and held + extra > self._memory // KEY_SHARE
        if over_share or self._bytes + extra > self._memory:
            return _FULL
        self._bytes += extra
        self._by_owner[owner] = held + extra
        return None

    def _leave(self, owner: str, weight: int) -> None:
        self._requests -= 1
        self._bytes -= weight
        left = self._by_owner.get(owner, 0) - weight
        if left > 0:
            self._by_owner[owner] = left
        else:
            self._by_owner.pop(owner, None)


_FULL = Refusal(
    ErrorCode.RATE_LIMITED,
    "The gateway is at its concurrency limit; retry shortly.",
    details={"limit": LimitName.CONCURRENCY.value},
    retry_after_seconds=_RETRY_HINT_SECONDS,
)


def content_length(headers: Mapping[str, str]) -> int | Refusal | None:
    """The declared length; a malformed value is refused rather than ignored."""
    raw = headers.get("content-length")
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdigit():
        return Refusal(ErrorCode.INVALID_REQUEST, "Content-Length is not a number.")
    return int(raw)


class ClientGone(Exception):
    """The client left before its body was read; the HTTP adapter raises it (no money moved)."""


@dataclass(frozen=True, slots=True)
class BodyTimeout(Refusal):
    """The body did not arrive in time or fast enough: `invalid_request` with HTTP 408."""


async def read_capped(
    chunks: AsyncIterator[bytes],
    limit: int,
    timeout_s: float | None = None,
    now: Callable[[], float] | None = None,
    reserve: Callable[[int], Refusal | None] | None = None,
) -> bytearray | Refusal:
    """Read the body, stopping the moment it passes `limit` (a lying or absent Content-Length).

    `timeout_s` bounds the whole read and the body must keep arriving (`PROGRESS_MIN_BYTES` per
    `PROGRESS_WINDOW_SECONDS`): a stalled or trickling upload gives its place back (SR-02).
    `reserve(total)` is asked for gate memory before each chunk is kept; a refusal ends the read.
    """
    clock = now or asyncio.get_running_loop().time
    buffer = bytearray()
    try:
        async with asyncio.timeout(timeout_s):
            window_start, window_bytes = clock(), 0
            async for chunk in chunks:
                if len(buffer) + len(chunk) > limit:
                    return too_large(limit)
                if reserve is not None and (refusal := reserve(len(buffer) + len(chunk))):
                    return refusal
                buffer += chunk
                window_bytes += len(chunk)
                if clock() - window_start >= PROGRESS_WINDOW_SECONDS:
                    if window_bytes < PROGRESS_MIN_BYTES:
                        return _TOO_SLOW
                    window_start, window_bytes = clock(), 0
    except TimeoutError:
        return _TOO_SLOW
    return buffer


_TOO_SLOW = BodyTimeout(ErrorCode.INVALID_REQUEST, "The request body was not received in time.")


@dataclass(frozen=True, slots=True)
class UnsupportedMedia(Refusal):
    """A wrong `Content-Type` on a body route: `invalid_request` with HTTP 415 (protocol 2.1)."""


UNSUPPORTED_MEDIA_STATUS = 415
BODY_TIMEOUT_STATUS = 408


def status_of(refusal: Refusal) -> int | None:
    """An HTTP status that differs from the refusal code's own, or None."""
    if isinstance(refusal, BodyTimeout):
        return BODY_TIMEOUT_STATUS
    return UNSUPPORTED_MEDIA_STATUS if isinstance(refusal, UnsupportedMedia) else None


def parse_body(
    endpoint: Endpoint, headers: Mapping[str, str], raw: bytes | bytearray
) -> Inbound | Refusal:
    """JSON object, or multipart for transcriptions; any other media type is `invalid_request`."""
    content_type = headers.get("content-type", "")
    media = content_type.split(";", 1)[0].strip().lower()
    if endpoint.multipart:
        if media != "multipart/form-data":
            return UnsupportedMedia(
                ErrorCode.INVALID_REQUEST, "Content-Type must be multipart/form-data."
            )
        parsed = parse_multipart(content_type, raw)
        if isinstance(parsed, Refusal):
            return parsed
        return Inbound(endpoint, headers, parsed[0], parsed[1])
    if media != "application/json":
        return UnsupportedMedia(ErrorCode.INVALID_REQUEST, "Content-Type must be application/json.")
    body = _json_object(raw)
    return body if isinstance(body, Refusal) else Inbound(endpoint, headers, body)


def _json_object(raw: bytes | bytearray) -> dict[str, Json] | Refusal:
    try:
        body: Json = json.loads(raw)
    except ValueError:
        return Refusal(ErrorCode.INVALID_REQUEST, "The request body is not valid JSON.")
    except RecursionError:  # nesting deeper than the interpreter allows (SR-07)
        return Refusal(ErrorCode.INVALID_REQUEST, "The request body is nested too deeply.")
    if not isinstance(body, dict):
        return Refusal(ErrorCode.INVALID_REQUEST, "The request body must be a JSON object.")
    return body
