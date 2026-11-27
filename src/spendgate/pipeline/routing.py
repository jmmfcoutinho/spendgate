"""Step 8: the failover loop inside one reservation.

Candidates and their cooldowns come from `pipeline.candidates` (the `Router`); the result types
and the refusal of a request that ends without an answer from `pipeline.forward_results`. This
module is the loop itself and the classification of each attempt. Failover happens only after an
attempt for which `not_processed` holds (never sent, or a `free_statuses` answer) and costs 0;
every other result ends the request (M-01/M-13). The provider's own 429 is never a budget error.

Streaming: a streaming request uses the same pick/attempt/failover code up to the
response head (`open_response`), classifies a non-2xx exactly like a buffered call, and on a 2xx
returns `Live` (the unread response plus its relay). The attempt stays in flight in the `Book`
until the stream ends; `pipeline.streaming` then records it and settles.

Waiting: when every untried candidate is cooling, the loop sleeps until the soonest one is back,
at most `MAX_WAIT_MS` per request and only inside the request deadline (ADR-0021). The hold stays
as it is while it waits: nothing was sent, so nothing about the money changes.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import httpx

from spendgate.core.catalog import Catalog
from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.failures import AttemptError, CallErrorCode
from spendgate.core.outcomes import Outcome
from spendgate.ledger.port import AttemptRecord
from spendgate.pipeline.candidates import Cause, Router, Target, retry_hint
from spendgate.pipeline.faults import Fault, fault_of
from spendgate.pipeline.forward_results import (
    Answered,
    Failed,
    Job,
    Retry,
    exhausted,
    failure_for,
    unavailable,
)
from spendgate.pipeline.model import AttemptEvent, Clock, Observer, Sender
from spendgate.pipeline.settle import Book, Flight, Reading, attempt_record, read_usage
from spendgate.pipeline.streaming import Live
from spendgate.pipeline.transport import SendError, SendFailure, exchange, open_response, read_body
from spendgate.providers.port import SseLimits, Untranslatable, UpstreamError, UpstreamResponse

_MS = 1000
_HTTP_OK = 200
_HTTP_REDIRECT = 300
_HTTP_ERROR = 400
_HTTP_SERVER_ERROR = 500
_MAX_ERROR_BYTES = 1024 * 1024  # an error answer to a streaming request is read whole, capped
_HTTP_UNAUTHORIZED = 401  # only our credential is sent, so a 401 is never the caller's doing
_HTTP_TIMEOUT = 408
# Longest a request waits for a cooling deployment. At least `HealthConfig.throttle_max_ms`, so a
# 429 without Retry-After is always outwaited; a longer Retry-After is refused at once instead.
MAX_WAIT_MS = 1_000


class Forwarder:
    """Step 8: translate, send, classify, fail over. Records every attempt in the `Book`."""

    def __init__(  # noqa: PLR0913, PLR0917 - the loop's collaborators
        self,
        sender: Sender,
        router: Router,
        catalog: Catalog,
        now: Clock,
        observer: Observer,
        max_attempts: int,
        limits: SseLimits = SseLimits(),  # noqa: B008 - frozen default
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        deadline_ms: float | None = None,
    ) -> None:
        self._sender, self._router, self._catalog = sender, router, catalog
        self._now, self._observer, self._max = now, observer, max_attempts
        self._limits, self._sleep, self._deadline_ms = limits, sleep, deadline_ms

    async def run(self, job: Job, book: Book) -> Answered | Failed | Live:
        tried: set[str] = set()
        retries: list[Retry] = []
        last: Target | None = None
        attempts = waited = 0
        start = self._now()
        while attempts < self._max:
            target = self._router.pick(job.targets, tried, self._now())
            if target is None:
                target = self._same_again(retries, last)
            if target is None:
                pause = self._pause(job, tried, waited, start)
                if pause is None:
                    break
                await self._sleep(pause / _MS)  # a wait costs no attempt: nothing is sent
                waited += pause
                continue
            attempts += 1
            tried.add(target.label)
            last = target
            step = await self._attempt(job, book, target)
            if not isinstance(step, Retry):
                return step
            retries.append(step)
        return exhausted(self._router, self._now, job, book, retries)

    def _pause(self, job: Job, tried: set[str], waited: int, start: int) -> int | None:
        """Ms to wait for the soonest cooling candidate, or None: refuse now.

        Each waiter adds its own jitter, so callers waiting on one deployment do not all wake
        and send in the same instant; the pause is still clipped to what is left of
        `MAX_WAIT_MS`. A wait must leave a full `MAX_WAIT_MS` before the request deadline, or
        the call could be sent with no time left and be charged in full. Every pause is at
        least 1 ms and adds to `waited`, so the loop ends even if the clock stands still.
        """
        now, left = self._now(), MAX_WAIT_MS - waited
        soonest = self._router.soonest(job.targets, tried, now)
        if soonest is None or soonest > left:
            return None
        pause = min(soonest + self._router.jitter(), left)
        if self._deadline_ms is not None and now + pause + MAX_WAIT_MS > start + self._deadline_ms:
            return None
        return pause

    def _same_again(self, retries: list[Retry], last: Target | None) -> Target | None:
        """No other candidate: a connect failure (never sent) is tried once more on the same one."""
        if last is None or not retries or retries[-1].cause is not Cause.UNREACHABLE:
            return None
        return last if last.shared.health.allow(self._now()) else None

    async def _attempt(
        self, job: Job, book: Book, target: Target
    ) -> Retry | Answered | Failed | Live:
        try:
            # Token refreshes are blocking network calls: `prepare` runs them in a worker thread,
            # so a slow identity provider never stalls the other requests on this event loop.
            await job.provider.prepare(target.deployment)
            upstream = job.provider.translate(job.request, target.deployment)
        except Untranslatable as refused:
            return Failed(refused.refusal, 400)  # never sent: nothing recorded, hold released
        except Exception as exc:
            internal = Refusal(ErrorCode.INTERNAL_ERROR, "The upstream request could not be built.")
            return Failed(internal, 500, fault=fault_of("routing.translate", exc))
        started = self._now()
        book.flight = Flight(
            book.next_number, target.provider, target.label, started, target.deployment.region
        )
        try:
            if job.request.stream:
                opened = await open_response(self._sender, upstream)
                if _HTTP_OK <= opened.status_code < _HTTP_REDIRECT:
                    return await self._live(job, book, target, started, opened)
                response = await read_body(opened, _MAX_ERROR_BYTES)
            else:
                response = await exchange(self._sender, upstream)
        except SendError as exc:
            return self._send_failed(job, book, target, started, exc.failure)
        # The attempt stays in flight until `_record` writes its outcome: whatever raises between
        # here and there (an adapter, say) is charged as a sent call, never released (CR-01).
        if _HTTP_OK <= response.status < _HTTP_REDIRECT:
            return self._answered(job, book, target, started, response)
        return self._rejected(job, book, target, started, response)

    async def _live(
        self, job: Job, book: Book, target: Target, started: int, opened: httpx.Response
    ) -> Live | Failed:
        """A 2xx stream head: the attempt stays in flight (sent, unfinished) until it ends."""
        try:
            relay = job.provider.relay(job.request, self._limits)
        except Exception as exc:
            await opened.aclose()  # the request was sent: charged at the full hold
            self._record(
                job,
                book,
                target,
                started,
                Outcome.INTERNAL_ERROR,
                opened.status_code,
                not_processed=False,
                error_code=AttemptError.RELAY_ERROR,
            )
            refusal = Refusal(ErrorCode.INTERNAL_ERROR, "The stream could not be relayed.")
            return Failed(refusal, 500, fault=fault_of("routing.relay", exc))
        self._router.succeeded(target, self._now(), started)
        broke = lambda: self._router.failed(target, self._now())  # noqa: E731 - a one-line callback
        return Live(
            opened,
            relay,
            job.model,
            target.provider,
            target.label,
            started,
            broke,
            target.deployment.region,
        )

    # --- outcomes of one attempt ---------------------------------------------------------------

    def _record(  # noqa: PLR0913, PLR0917 - one attempt's facts
        self,
        job: Job,
        book: Book,
        target: Target,
        started: int,
        outcome: Outcome,
        status: int | None,
        *,
        not_processed: bool,
        cooldown_ms: int | None = None,
        error_code: CallErrorCode | None = None,
        reading: Reading | None = None,
    ) -> AttemptRecord:
        """Write the attempt's row and end its flight: the outcome is now recorded."""
        now = self._now()
        flight = Flight(
            book.next_number, target.provider, target.label, started, target.deployment.region
        )
        record = attempt_record(
            book,
            flight,
            outcome,
            now,
            request_id=job.request_id,
            status=status,
            not_processed=not_processed,
            error_code=error_code,
            reading=reading,
        )
        book.attempts.append(record)
        book.flight = None
        self._observer_safe(
            AttemptEvent(
                job.request_id,
                job.model,
                target.provider,
                target.label,
                outcome,
                status,
                not_processed,
                record.latency_ms or 0,
                cooldown_ms,
            )
        )
        return record

    def _observer_safe(self, event: AttemptEvent) -> None:
        with contextlib.suppress(Exception):
            self._observer.on_attempt(event)

    def _send_failed(
        self, job: Job, book: Book, target: Target, started: int, failure: SendFailure
    ) -> Retry | Failed:
        now = self._now()
        if not failure.sent:  # connect/DNS/TLS before the request left: free, fail over
            # No cooldown: a slow DNS answer is not the provider asking us to wait. It counts
            # towards the breaker, which opens only at the failure-rate threshold.
            self._router.failed(target, now)
            self._record(
                job,
                book,
                target,
                started,
                Outcome.UNREACHABLE,
                None,
                not_processed=True,
                error_code=failure.code,
            )
            return Retry(Cause.UNREACHABLE, None)
        self._router.failed(target, now)
        outcome = Outcome.TIMEOUT if failure.timeout else Outcome.UPSTREAM_5XX
        self._record(
            job,
            book,
            target,
            started,
            outcome,
            None,
            not_processed=False,
            error_code=failure.code,
        )
        if failure.timeout:
            return unavailable(target.provider, "The provider timed out.", 504)
        return unavailable(target.provider, "The provider connection failed.", 502)

    def _unreadable(  # noqa: PLR0913, PLR0917 - one attempt's facts and the trace
        self, job: Job, book: Book, target: Target, started: int, status: int, fault: Fault
    ) -> Failed:
        """An adapter raised on a sent request: usage is unreadable, so the full hold (CR-01)."""
        self._router.failed(target, self._now())
        self._record(
            job,
            book,
            target,
            started,
            Outcome.INTERNAL_ERROR,
            status,
            not_processed=False,
            error_code=AttemptError.ADAPTER_ERROR,
        )
        internal = Refusal(ErrorCode.INTERNAL_ERROR, "The provider answer could not be read.")
        return Failed(internal, 500, fault=fault)

    def _answered(
        self, job: Job, book: Book, target: Target, started: int, response: UpstreamResponse
    ) -> Answered | Failed:
        """A 2xx: usage is read from the raw answer, then the body is put in the client's shape."""
        try:
            reading = read_usage(
                self._catalog, book.pricing.entry, job.provider.usage(job.request, response)
            )
        except Exception as exc:  # an adapter must not raise, but if it does it was still sent
            fault = fault_of("routing.usage", exc)
            return self._unreadable(job, book, target, started, response.status, fault)
        book.note(reading)
        outcome = Outcome.OK if reading.cost else Outcome.OK_NO_USAGE
        self._router.succeeded(target, self._now(), started)
        self._record(
            job,
            book,
            target,
            started,
            outcome,
            response.status,
            not_processed=False,
            reading=reading,
        )
        try:
            converted = job.provider.response(job.request, response)
        except Exception as exc:  # recorded as answered: the call is charged, the client gets 500
            internal = Refusal(ErrorCode.INTERNAL_ERROR, "The provider answer could not be read.")
            return Failed(internal, 500, fault=fault_of("routing.response", exc))
        if converted.status >= _HTTP_ERROR:  # an unreadable 2xx: the adapter gave a bodiless 502
            return unavailable(target.provider, "The provider answer could not be read.", 502)
        return Answered(converted)

    def _rejected(
        self, job: Job, book: Book, target: Target, started: int, response: UpstreamResponse
    ) -> Retry | Failed:
        """A non-2xx answer: failover only when `not_processed`, otherwise it ends the request."""
        status = response.status
        if status < _HTTP_ERROR:  # a redirect or 1xx we never asked for: charged, fail closed
            self._router.failed(target, self._now())
            self._record(
                job, book, target, started, Outcome.UPSTREAM_5XX, status, not_processed=False
            )
            return unavailable(target.provider, "The provider answered unexpectedly.", 502)
        try:
            err = job.provider.error(response)
        except Exception as exc:  # unknown what the provider did with it: charged, fail closed
            fault = fault_of("routing.error", exc)
            return self._unreadable(job, book, target, started, status, fault)
        free = job.free_statuses
        now = self._now()
        if err.not_processed(free):
            quota = err.code is ErrorCode.UPSTREAM_UNAVAILABLE  # e.g. insufficient_quota on a 429
            cause = Cause.QUOTA if quota else Cause.THROTTLED
            retry_s, unknown = retry_hint(err, response.headers, now)
            cooldown = self._router.cool(target, now, retry_s, cause, unknown=unknown)
            self._record(
                job,
                book,
                target,
                started,
                err.outcome(free),
                status,
                not_processed=True,
                cooldown_ms=cooldown,
                error_code=err.code,
            )
            return Retry(cause, retry_s)
        self._feed_health(target, _everyones_fault(status, err), now, started)
        self._record(
            job,
            book,
            target,
            started,
            err.outcome(free),
            status,
            not_processed=False,
            error_code=err.code,
        )
        return failure_for(err, target.provider)

    def _feed_health(self, target: Target, everyones: bool, now: int, started: int) -> None:
        if everyones:
            self._router.failed(target, now)
        else:
            self._router.succeeded(target, now, started)  # it answered: the request was bad


def _everyones_fault(status: int, err: UpstreamError) -> bool:
    """Whether a sent, answered error counts towards the deployment's shared breaker.

    Only what would fail for every caller does: the provider's 5xx or 408, a 401, or a code the
    adapter reads as our credential or deployment being wrong (`deployment_fault`). Any other 4xx,
    a 403 or 404 included, answered this one request: counting it would let one key take the
    deployment away from every other key (ADR-0033).
    """
    if status >= _HTTP_SERVER_ERROR or status in {_HTTP_TIMEOUT, _HTTP_UNAUTHORIZED}:
        return True
    return err.deployment_fault
