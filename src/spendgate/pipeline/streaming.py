"""Streamed answers: relay the bytes, settle exactly once on every ending.

How one stream flows. `Forwarder` opens the upstream response (head only), classifies a non-2xx
like a buffered call, and on a 2xx returns a `Live` holding the response and one `StreamRelay`.
`Pipeline` wraps it in a `StreamBody`, the object the HTTP adapter iterates. Each step reads one
upstream chunk, feeds the relay and yields what the relay returns; nothing is buffered beyond the
chunk in hand. The end of the stream, a cut, a limit breach, a lost lease or a gone client all end
in `StreamBody._settle`, which books ONE attempt row and ONE ledger transition from a task that a
cancel cannot interrupt:

| Ending                                 | Outcome         | Charge                         |
|----------------------------------------|-----------------|--------------------------------|
| upstream ends, final usage and priced  | `ok`            | actual usage                   |
| upstream ends, no usage / unpriced     | `ok_no_usage`   | full hold                      |
| cut, bad framing, SSE limit, timeout   | `stream_broken` | final usage if seen, else hold |
| lease lost or about to expire          | `stream_broken` | same                           |
| kill switch engaged, session time over | `stream_broken` | same                           |
| longer than `max_request_seconds`      | `stream_broken` | same                           |
| client disconnects / body closed early | `cancelled`     | same                           |

The upstream call is closed before the settlement is booked. Cost reaches the client as one SSE
comment after settlement (protocol 5: a stream has no cost header); a broken stream gets one
`error` event shaped per endpoint instead, then (chat only) `[DONE]`.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass

import httpx

from spendgate.core.catalog import Catalog
from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.estimate import RequestKind
from spendgate.core.failures import AttemptError, CallErrorCode, StopReason
from spendgate.core.outcomes import Outcome
from spendgate.pipeline import replies
from spendgate.pipeline.faults import Fault, InternalEvent, fault_of
from spendgate.pipeline.leases import Leases
from spendgate.pipeline.model import AttemptEvent, Clock, Endpoint, Observer, report_internal
from spendgate.pipeline.settle import Book, Closed, Flight, attempt_record, read_usage
from spendgate.pipeline.transport import classify
from spendgate.providers.port import SseLimits, StreamBroken, StreamLimitExceeded, StreamRelay

# Stream limits (threat-model C-22): the numbers the port left open, enforced by the relay while it
# parses, so a hostile stream stops before it is buffered.
STREAM_LIMITS = SseLimits(
    max_line_bytes=64 * 1024,
    max_event_bytes=1024 * 1024,
    max_events=100_000,
    max_stream_bytes=64 * 1024 * 1024,
)
PING_SECONDS = 15.0  # idle keep-alive comment interval (protocol 2.8)
_PING = b": ping\n\n"
_DONE = b"data: [DONE]\n\n"
_IDLE = object()
_BAD_GATEWAY = 502
_LOST_MESSAGE = "The stream was stopped because its budget hold could not be kept alive."
_TOO_LONG_MESSAGE = "The stream was stopped because it ran longer than the gateway allows."
# Total over `StopReason` (a test checks it), so a new reason cannot reach the client unworded.
_STOP_MESSAGES: Mapping[StopReason, str] = {
    StopReason.LEASE_EXPIRING: _LOST_MESSAGE,
    StopReason.LEASE_LOST: _LOST_MESSAGE,
    StopReason.RENEWAL_FAILED: _LOST_MESSAGE,
    StopReason.MAX_DURATION: _TOO_LONG_MESSAGE,
    StopReason.KILL_SWITCH: "The stream was stopped because a kill switch was engaged.",
    StopReason.SESSION_TIME: "The stream was stopped because its session's time limit passed.",
    StopReason.CLIENT_GONE: "The stream was stopped because its client went away.",
}
_CUT_MESSAGE = "The provider stream ended abnormally."


@dataclass(frozen=True, slots=True)
class Live:
    """An open upstream 2xx: the response (body unread), its relay and the attempt's identity."""

    response: httpx.Response
    relay: StreamRelay
    model: str  # catalog id, for events
    provider: str
    deployment: str
    started: int
    broke: Callable[[], None]  # health feedback when the provider side cut the stream
    region: str | None = None  # the deployment's, for the price id on the row


@dataclass(frozen=True, slots=True)
class StreamEnv:
    """What a `StreamBody` needs from the pipeline (so it never imports `Pipeline`)."""

    catalog: Catalog
    now: Clock
    observer: Observer
    leases: Leases
    close: Callable[[Book, int | None], Awaitable[Closed]]  # the one ledger transition
    spawn: Callable[[Awaitable[Closed]], "asyncio.Future[Closed]"]
    ping_seconds: float = PING_SECONDS
    max_ms: int | None = None  # longest a stream may run from its hold (SR-13)


async def _next(source: AsyncIterator[bytes]) -> object:
    """The next chunk, or None at the end (a task cannot carry StopAsyncIteration cleanly)."""
    try:
        return await anext(source)
    except StopAsyncIteration:
        return None


async def _woken(event: asyncio.Event) -> object:
    await event.wait()
    return None


class StreamBody:
    """The live stream the HTTP adapter iterates; `aclose()` MUST be called when it stops."""

    def __init__(  # noqa: PLR0913, PLR0917 - the stream's collaborators
        self,
        env: StreamEnv,
        request_id: str,
        endpoint: Endpoint,
        book: Book,
        live: Live,
        held_at: int,
    ) -> None:
        self._env, self._id, self._endpoint = env, request_id, endpoint
        self._book, self._live, self._held_at = book, live, held_at
        self._source = live.response.aiter_bytes()
        self._out: list[bytes] = []
        self._wake = asyncio.Event()  # set by `abort`
        self._reason: StopReason | None = None  # why the gateway stops the stream
        self._pending: asyncio.Future[object] | None = None  # the read in progress
        self._waiter: asyncio.Future[object] | None = None  # resolves when `abort` is called
        self._closing: asyncio.Future[Closed] | None = None
        self._ended = False
        self._stepping = False  # a consumer is inside `__anext__`; it acts on an abort itself
        self._cut = False  # the provider side broke the stream (health feedback)
        self._events = 0  # client events sent so far (a Responses error's sequence number)
        self._tail = b""  # chat's `[DONE]`, held back so the cost comment can precede it
        self._first_upstream: int | None = None
        self._ttft: int | None = None
        self._fault: Fault | None = None  # an adapter's surprise, reported once it is settled
        deadline = held_at + env.max_ms if env.max_ms is not None else None
        env.leases.register(request_id, held_at, self, deadline)

    # --- the iterator the adapter drives -------------------------------------------------------

    def __aiter__(self) -> "StreamBody":
        return self

    async def __anext__(self) -> bytes:
        self._stepping = True
        try:
            while not self._out:
                if self._ended:
                    raise StopAsyncIteration
                await self._step()
        except asyncio.CancelledError:  # the client went away: abort upstream, settle, re-raise
            await self._settle(Outcome.CANCELLED, StopReason.CLIENT_GONE)
            raise
        finally:
            self._stepping = False
        if self._ttft is None:
            self._ttft = max(self._env.now() - self._held_at, 0)
        return self._out.pop(0)

    async def aclose(self) -> None:
        """The stream stopped (done, client gone, error): idempotent, always settles."""
        await self._settle(Outcome.CANCELLED, StopReason.CLIENT_GONE)
        self._ended = True

    def abort(self, reason: StopReason) -> None:
        """Ask the stream to stop (lease problems, kill switch, session clock, deadline).

        A consumer inside a step acts on it at once. Otherwise (the client stopped reading, or
        never started) nobody would: the stream is settled here as broken (NF-02).
        """
        first = self._reason is None
        self._reason = self._reason or reason
        self._wake.set()
        if first and not self._stepping and self._closing is None:
            self._env.spawn(self._settle(Outcome.STREAM_BROKEN, reason))

    # --- one step -------------------------------------------------------------------------------

    async def _step(self) -> None:
        reason = self._reason
        if reason is None and self._env.leases.near_expiry(self._id, self._env.now()):
            reason = StopReason.LEASE_EXPIRING
        limit = self._env.max_ms
        if reason is None and limit is not None and self._env.now() - self._held_at >= limit:
            reason = StopReason.MAX_DURATION
        if reason is not None:
            await self._break(reason, _STOP_MESSAGES[reason])
            return
        try:
            data = await self._read()
        except Exception as exc:  # the provider side failed mid-body: sent, so charged
            self._cut = True
            await self._break(classify(exc).code, _CUT_MESSAGE)
            return
        if data is _IDLE:
            if self._reason is None:  # quiet interval; a wake is handled by the next step
                self._out.append(_PING)
        elif data is None:
            await self._end()
        elif isinstance(data, bytes):
            await self._relay(data)

    async def _read(self) -> "bytes | object | None":
        """The next upstream chunk; None at the end; `_IDLE` after a quiet interval or a wake."""
        pending = self._pending or asyncio.ensure_future(_next(self._source))
        waiter = self._waiter or asyncio.ensure_future(_woken(self._wake))
        self._pending, self._waiter = pending, waiter
        done, _ = await asyncio.wait(
            {pending, waiter}, timeout=self._env.ping_seconds, return_when=asyncio.FIRST_COMPLETED
        )
        if pending not in done:
            return _IDLE
        self._pending = None
        if self._first_upstream is None:
            self._first_upstream = self._env.now()
        chunk = pending.result()
        return chunk if isinstance(chunk, bytes) else None

    async def _relay(self, data: bytes) -> None:
        try:
            self._emit(self._live.relay.feed(data))
        except StreamBroken as exc:  # malformed framing or an SSE limit (C-22)
            self._cut = True
            await self._break(cut_code(exc), _CUT_MESSAGE)
        except Exception as exc:
            self._fault = fault_of("stream.relay", exc)
            await self._break(AttemptError.RELAY_ERROR, "The stream could not be relayed.")

    async def _end(self) -> None:
        try:
            self._emit(self._live.relay.close())
        except StreamBroken as exc:  # the body ended inside an event: a cut
            self._cut = True
            await self._break(cut_code(exc), _CUT_MESSAGE)
            return
        closed = await self._settle(None, None)
        if closed.booked:  # else the sweeper charges it and there is no number to show
            self._out.append(self._comment(closed))
        self._finish()

    def _emit(self, out: bytes) -> None:
        if out.endswith(_DONE) and self._endpoint.kind is RequestKind.CHAT:
            out, self._tail = out[: -len(_DONE)], _DONE  # the cost comment goes before [DONE]
        self._events += out.count(b"\n\n")
        if out:
            self._out.append(out)

    def _finish(self) -> None:
        if self._tail:
            self._out.append(self._tail)
            self._tail = b""
        self._ended = True

    def _comment(self, closed: Closed) -> bytes:
        parts = [f"request_id={self._id}", f"cost={closed.decision.charge.format()}"]
        if closed.remaining is not None:
            parts += [f"remaining={closed.remaining[0].format()}", f"scope={closed.remaining[1]}"]
        return f": spendgate {' '.join(parts)}\n\n".encode()

    # --- a broken stream ------------------------------------------------------------------------

    async def _break(self, code: CallErrorCode, message: str) -> None:
        """Settle as `stream_broken` and tell the client with one error event, then end."""
        await self._settle(Outcome.STREAM_BROKEN, code)
        self._out.append(self._error_event(message))
        if self._endpoint.kind is RequestKind.CHAT and not self._tail:
            self._tail = _DONE
        self._finish()

    def _error_event(self, message: str) -> bytes:
        """Protocol 2.8: one error event shaped per endpoint (status 200 is already sent)."""
        shape = self._endpoint.shape
        if self._endpoint.kind is RequestKind.RESPONSES:
            payload: Mapping[str, object] = {
                "type": "error",
                "code": ErrorCode.UPSTREAM_UNAVAILABLE.value,
                "message": message,
                "param": None,
                "sequence_number": self._events,
            }
            return b"event: error\ndata: " + _dump(payload) + b"\n\n"
        refusal = Refusal(ErrorCode.UPSTREAM_UNAVAILABLE, message)
        body = replies.error_body(refusal, shape, self._id, _BAD_GATEWAY)
        head = b"event: error\n" if shape == "anthropic" else b""
        return head + b"data: " + body + b"\n\n"

    # --- settlement -----------------------------------------------------------------------------

    async def _settle(self, outcome: Outcome | None, code: CallErrorCode | None) -> Closed:
        """Close upstream, book the attempt, make the one ledger transition. Exactly once.

        The work runs in a spawned task and callers wait on a shield: a second cancel of the
        client's task cannot stop it, and a second ending returns the first one's result.
        """
        if self._closing is None:
            for task in (self._pending, self._waiter):
                if task is not None:
                    task.cancel()
            self._env.leases.unregister(self._id)
            self._closing = self._env.spawn(self._close(outcome, code))
        return await asyncio.shield(self._closing)

    async def _close(self, outcome: Outcome | None, code: CallErrorCode | None) -> Closed:
        pending = [t for t in (self._pending, self._waiter) if t is not None]
        await asyncio.gather(*pending, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self._live.response.aclose()  # drops the upstream connection: the call is aborted
        try:
            self._record(outcome, code)
        finally:  # NF-04: the one transition always runs; a flight left open means the full hold
            closed = await self._env.close(self._book, self._ttft)
        if self._fault is not None:
            report_internal(
                self._env.observer, InternalEvent(self._id, self._fault, closed.charged)
            )
        return closed

    def _record(self, outcome: Outcome | None, code: CallErrorCode | None) -> None:
        """A stream's attempt row is written at its end, when usage is finally known.

        `relay.usage()` is the stream's FINAL usage or None (the `StreamRelay` contract): a cut,
        cancel or gateway stop before the end-of-stream usage arrived is charged the full hold
        (M-13), and usage above the hold is still booked as reported (M-03).
        """
        book, live, now = self._book, self._live, self._env.now()
        usage = None
        try:
            usage = live.relay.usage()
        except Exception as exc:  # an adapter that raises is unreadable usage: the full hold
            self._fault = self._fault or fault_of("stream.usage", exc)
        reading = read_usage(self._env.catalog, book.pricing.entry, usage)
        book.note(reading)
        outcome = outcome or (Outcome.OK if reading.cost else Outcome.OK_NO_USAGE)
        if self._cut:
            live.broke()
        first = self._first_upstream
        number = book.flight.number if book.flight else book.next_number
        record = attempt_record(
            book,
            Flight(number, live.provider, live.deployment, live.started, live.region),
            outcome,
            now,
            request_id=self._id,
            status=live.response.status_code,
            error_code=code,
            reading=reading,
            streamed=True,
            ttfb_ms=max(first - live.started, 0) if first is not None else None,
        )
        book.attempts.append(record)
        book.flight = None
        with contextlib.suppress(Exception):  # observers never affect money handling
            self._env.observer.on_attempt(
                AttemptEvent(
                    self._id,
                    live.model,
                    live.provider,
                    live.deployment,
                    outcome,
                    record.http_status,
                    False,
                    record.latency_ms or 0,
                )
            )


def cut_code(exc: StreamBroken) -> AttemptError:
    """A crossed limit is told apart from bad framing (a too-large frame is both: the limit)."""
    if isinstance(exc, StreamLimitExceeded):
        return AttemptError.STREAM_LIMIT_EXCEEDED
    return AttemptError.STREAM_MALFORMED


def _dump(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()
