"""Step 4 and 8: which deployment to try, and the failover loop inside one reservation.

Candidates are the deployments of one alias (one catalog id) in config order. `DeploymentHealth`
is the only tracker of cooldown and circuit state. Failover happens only after an attempt for
which `not_processed` holds (never sent, or a `free_statuses` answer) and costs 0; every other
result ends the request (M-01/M-13). The provider's own 429 is never a budget error.

Streaming: a streaming request uses the same pick/attempt/failover code up to the
response head (`open_response`), classifies a non-2xx exactly like a buffered call, and on a 2xx
returns `Live` (the unread response plus its relay). The attempt stays in flight in the `Book`
until the stream ends; `pipeline.streaming` then records it and settles.
"""

import contextlib
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

import httpx

from spendgate.config import ResolvedDeployment, Route
from spendgate.core.catalog import Catalog
from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.health import DeploymentHealth, HealthConfig
from spendgate.core.outcomes import Outcome
from spendgate.ledger.port import AttemptRecord
from spendgate.pipeline.model import AttemptEvent, Clock, Observer, Sender
from spendgate.pipeline.settle import Book, Flight, Reading, attempt_record, read_usage
from spendgate.pipeline.streaming import Live
from spendgate.pipeline.transport import SendError, SendFailure, exchange, open_response, read_body
from spendgate.providers.port import (
    AuthMode,
    CleanRequest,
    Deployment,
    Provider,
    SseLimits,
    Untranslatable,
    UpstreamError,
    UpstreamResponse,
)

_MS = 1000
_HTTP_OK = 200
_HTTP_REDIRECT = 300
_HTTP_ERROR = 400
_MAX_ERROR_BYTES = 1024 * 1024  # an error answer to a streaming request is read whole, capped
_OUR_CREDENTIAL_FAULTS = frozenset({401, 403, 404})  # the deployment is misconfigured, not busy


class Cause(StrEnum):
    THROTTLED = "throttled"
    QUOTA = "quota"  # a free 429 that means the account's quota or billing, not a busy model
    UNREACHABLE = "unreachable"
    FAILING = "failing"


@dataclass(slots=True)
class Shared:
    """Health of one real endpoint, shared by every alias that routes to it."""

    health: DeploymentHealth
    cause: Cause | None = None


@dataclass(frozen=True, slots=True)
class Target:
    label: str  # `<alias>/<index>`: stable, free of secrets, written to the calls table
    provider: str
    deployment: Deployment
    shared: Shared


class Router:
    """Ordered candidates per alias plus the cooldown bookkeeping."""

    def __init__(self, routes: Mapping[str, Route], config: HealthConfig) -> None:
        self._config = config
        shared: dict[ResolvedDeployment, Shared] = {}
        self._targets: dict[str, tuple[Target, ...]] = {}
        for alias, route in routes.items():
            self._targets[alias] = tuple(
                Target(
                    f"{alias}/{i}",
                    d.provider,
                    _deployment(d),
                    shared.setdefault(d, Shared(DeploymentHealth(config))),
                )
                for i, d in enumerate(route.deployments)
            )

    def targets(self, alias: str) -> tuple[Target, ...]:
        return self._targets[alias]

    def pick(self, targets: Iterable[Target], tried: set[str], now: int) -> Target | None:
        """The first untried deployment health allows (a half-open probe is taken only here)."""
        for target in targets:
            if target.label not in tried and target.shared.health.allow(now):
                return target
        return None

    def cool(self, target: Target, now: int, retry_after_s: int | None, cause: Cause) -> int:
        """Put the deployment into cooldown (honouring Retry-After, capped); returns ms."""
        wanted = max(self._config.cooldown_ms, (retry_after_s or 0) * _MS)
        target.shared.health.record_throttled(now, wanted)
        target.shared.cause = cause
        return min(wanted, self._config.max_cooldown_ms)

    def failed(self, target: Target, now: int) -> None:
        target.shared.health.record_failure(now)
        target.shared.cause = Cause.FAILING

    def succeeded(self, target: Target, now: int) -> None:
        target.shared.health.record_success(now)
        target.shared.cause = None

    def wait_seconds(self, targets: Iterable[Target], now: int) -> int:
        """Whole seconds until the soonest deployment may be tried again (at least 1)."""
        waits = [t.shared.health.retry_after(now) for t in targets]
        ms = min(waits, default=0) or self._config.cooldown_ms
        return max(math.ceil(ms / _MS), 1)

    def all_throttled(self, targets: Iterable[Target]) -> bool:
        return all(t.shared.cause in (Cause.THROTTLED, Cause.QUOTA) for t in targets)

    def all_quota(self, targets: Iterable[Target]) -> bool:
        return all(t.shared.cause is Cause.QUOTA for t in targets)


def _deployment(d: ResolvedDeployment) -> Deployment:
    return Deployment(
        d.provider, d.base_url, d.model, AuthMode(d.auth), d.region, d.api_version,
        d.credential.reveal() if d.credential is not None else "",
    )  # fmt: skip


# --- results -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Answered:
    """A successful provider answer, already in the client's shape."""

    response: UpstreamResponse


@dataclass(frozen=True, slots=True)
class Failed:
    """The request ended without an answer for the client."""

    refusal: Refusal
    status: int
    should_retry: bool = False


@dataclass(frozen=True, slots=True)
class _Retry:
    cause: Cause
    retry_after_s: int | None


@dataclass(frozen=True, slots=True)
class Job:
    """Everything the loop needs about one admitted request."""

    request_id: str
    request: CleanRequest
    provider: Provider
    targets: tuple[Target, ...]
    free_statuses: frozenset[int]
    model: str  # catalog id, for events


def _throttled(provider: str, retry_after_s: int) -> Failed:
    refusal = Refusal(
        ErrorCode.UPSTREAM_THROTTLED,
        "The provider is rate limiting this model; retry later.",
        details={"provider": provider},
        retry_after_seconds=retry_after_s,
    )
    return Failed(refusal, 429, should_retry=True)


def unavailable(provider: str, message: str, status: int, *, retry: bool = False) -> Failed:
    refusal = Refusal(ErrorCode.UPSTREAM_UNAVAILABLE, message, details={"provider": provider})
    return Failed(refusal, status, should_retry=retry)


class Forwarder:
    """Step 8: translate, send, classify, fail over. Records every attempt in the `Book`."""

    def __init__(  # noqa: PLR0913, PLR0917 - the loop's collaborators
        self, sender: Sender, router: Router, catalog: Catalog, now: Clock, observer: Observer,
        max_attempts: int, limits: SseLimits = SseLimits(),  # noqa: B008 - frozen default
    ) -> None:  # fmt: skip
        self._sender, self._router, self._catalog = sender, router, catalog
        self._now, self._observer, self._max = now, observer, max_attempts
        self._limits = limits

    async def run(self, job: Job, book: Book) -> Answered | Failed | Live:
        tried: set[str] = set()
        retries: list[_Retry] = []
        last: Target | None = None
        for _ in range(self._max):
            target = self._router.pick(job.targets, tried, self._now())
            if target is None:
                target = self._same_again(retries, last)
            if target is None:
                break
            tried.add(target.label)
            last = target
            step = await self._attempt(job, book, target)
            if not isinstance(step, _Retry):
                return step
            retries.append(step)
        return self._exhausted(job, book, retries)

    def _same_again(self, retries: list[_Retry], last: Target | None) -> Target | None:
        """No other candidate: a connect failure (never sent) is tried once more on the same one."""
        if last is None or not retries or retries[-1].cause is not Cause.UNREACHABLE:
            return None
        return last if last.shared.health.allow(self._now()) else None

    def _exhausted(self, job: Job, book: Book, retries: list[_Retry]) -> Failed:
        """Every attempt was not_processed (or none could be made): the hold is released."""
        provider = job.targets[0].provider
        hinted = [r.retry_after_s for r in retries if r.retry_after_s is not None]
        wait = max(hinted) if hinted else self._router.wait_seconds(job.targets, self._now())
        if retries:
            if all(r.cause is Cause.QUOTA for r in retries):  # retrying a billing fault is futile
                return unavailable(provider, "The provider could not serve the request.", 502)
            if any(r.cause in (Cause.THROTTLED, Cause.QUOTA) for r in retries):
                return _throttled(provider, wait)
            return unavailable(provider, "The provider could not be reached.", 502, retry=True)
        # Nothing was tried: every deployment is cooling or circuit-open.
        if self._router.all_quota(job.targets):
            book.idle_outcome = Outcome.THROTTLED
            return unavailable(provider, "The provider could not serve the request.", 502)
        if self._router.all_throttled(job.targets):
            book.idle_outcome = Outcome.THROTTLED
            return _throttled(provider, wait)
        book.idle_outcome = Outcome.UNREACHABLE
        refusal = Refusal(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "Every deployment is temporarily unavailable.",
            details={"provider": provider},
            retry_after_seconds=wait,
        )
        return Failed(refusal, 503, should_retry=True)

    async def _attempt(
        self, job: Job, book: Book, target: Target
    ) -> _Retry | Answered | Failed | Live:
        try:
            upstream = job.provider.translate(job.request, target.deployment)
        except Untranslatable as refused:
            return Failed(refused.refusal, 400)  # never sent: nothing recorded, hold released
        except Exception:
            internal = Refusal(ErrorCode.INTERNAL_ERROR, "The upstream request could not be built.")
            return Failed(internal, 500)
        started = self._now()
        book.flight = Flight(book.next_number, target.provider, target.label, started)
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
        except Exception:
            await opened.aclose()  # the request was sent: charged at the full hold
            self._record(job, book, target, started, Outcome.INTERNAL_ERROR, opened.status_code,
                         not_processed=False, error_code="relay_error")  # fmt: skip
            refusal = Refusal(ErrorCode.INTERNAL_ERROR, "The stream could not be relayed.")
            return Failed(refusal, 500)
        self._router.succeeded(target, self._now())
        broke = lambda: self._router.failed(target, self._now())  # noqa: E731 - a one-line callback
        return Live(opened, relay, job.model, target.provider, target.label, started, broke)

    # --- outcomes of one attempt ---------------------------------------------------------------

    def _record(  # noqa: PLR0913, PLR0917 - one attempt's facts
        self, job: Job, book: Book, target: Target, started: int, outcome: Outcome,
        status: int | None, *, not_processed: bool, cooldown_ms: int | None = None,
        error_code: str | None = None, reading: Reading | None = None,
    ) -> AttemptRecord:  # fmt: skip
        """Write the attempt's row and end its flight: the outcome is now recorded."""
        now = self._now()
        flight = Flight(book.next_number, target.provider, target.label, started)
        record = attempt_record(
            book, flight, outcome, now, request_id=job.request_id, status=status,
            not_processed=not_processed, error_code=error_code, reading=reading,
        )  # fmt: skip
        book.attempts.append(record)
        book.flight = None
        self._observer_safe(
            AttemptEvent(
                job.request_id, job.model, target.provider, target.label, outcome, status,
                not_processed, record.latency_ms or 0, cooldown_ms,
            )
        )  # fmt: skip
        return record

    def _observer_safe(self, event: AttemptEvent) -> None:
        with contextlib.suppress(Exception):
            self._observer.on_attempt(event)

    def _send_failed(
        self, job: Job, book: Book, target: Target, started: int, failure: SendFailure
    ) -> _Retry | Failed:
        now = self._now()
        if not failure.sent:  # connect/DNS/TLS before the request left: free, fail over
            # No cooldown: a slow DNS answer is not the provider asking us to wait. It counts
            # towards the breaker, which opens only at the failure-rate threshold.
            self._router.failed(target, now)
            self._record(
                job, book, target, started, Outcome.UNREACHABLE, None, not_processed=True,
                error_code=failure.kind,
            )  # fmt: skip
            return _Retry(Cause.UNREACHABLE, None)
        self._router.failed(target, now)
        outcome = Outcome.TIMEOUT if failure.timeout else Outcome.UPSTREAM_5XX
        self._record(
            job, book, target, started, outcome, None, not_processed=False,
            error_code=failure.kind,
        )  # fmt: skip
        if failure.timeout:
            return unavailable(target.provider, "The provider timed out.", 504)
        return unavailable(target.provider, "The provider connection failed.", 502)

    def _unreadable(
        self, job: Job, book: Book, target: Target, started: int, status: int
    ) -> Failed:
        """An adapter raised on a sent request: usage is unreadable, so the full hold (CR-01)."""
        self._router.failed(target, self._now())
        self._record(
            job, book, target, started, Outcome.INTERNAL_ERROR, status, not_processed=False,
            error_code="adapter_error",
        )  # fmt: skip
        internal = Refusal(ErrorCode.INTERNAL_ERROR, "The provider answer could not be read.")
        return Failed(internal, 500)

    def _answered(
        self, job: Job, book: Book, target: Target, started: int, response: UpstreamResponse
    ) -> Answered | Failed:
        """A 2xx: usage is read from the raw answer, then the body is put in the client's shape."""
        try:
            reading = read_usage(
                self._catalog, book.pricing.entry, job.provider.usage(job.request, response)
            )
        except Exception:  # an adapter must not raise, but if it does the call was still sent
            return self._unreadable(job, book, target, started, response.status)
        book.note(reading)
        outcome = Outcome.OK if reading.cost else Outcome.OK_NO_USAGE
        self._router.succeeded(target, self._now())
        self._record(
            job, book, target, started, outcome, response.status, not_processed=False,
            reading=reading,
        )  # fmt: skip
        try:
            converted = job.provider.response(job.request, response)
        except Exception:
            internal = Refusal(ErrorCode.INTERNAL_ERROR, "The provider answer could not be read.")
            return Failed(internal, 500)
        if converted.status >= _HTTP_ERROR:  # an unreadable 2xx: the adapter gave a bodiless 502
            return unavailable(target.provider, "The provider answer could not be read.", 502)
        return Answered(converted)

    def _rejected(
        self, job: Job, book: Book, target: Target, started: int, response: UpstreamResponse
    ) -> _Retry | Failed:
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
        except Exception:  # unknown what the provider did with it: charged, fail closed
            return self._unreadable(job, book, target, started, status)
        free = job.free_statuses
        now = self._now()
        if err.not_processed(free):
            quota = err.code is ErrorCode.UPSTREAM_UNAVAILABLE  # e.g. insufficient_quota on a 429
            cause = Cause.QUOTA if quota else Cause.THROTTLED
            cooldown = self._router.cool(target, now, err.retry_after_seconds, cause)
            self._record(
                job, book, target, started, err.outcome(free), status, not_processed=True,
                cooldown_ms=cooldown, error_code=err.code.value,
            )  # fmt: skip
            return _Retry(cause, err.retry_after_seconds)
        self._feed_health(target, status, now)
        self._record(
            job, book, target, started, err.outcome(free), status, not_processed=False,
            error_code=err.code.value,
        )  # fmt: skip
        return _failure_for(err, target.provider)

    def _feed_health(self, target: Target, status: int, now: int) -> None:
        if status >= 500 or status == 408 or status in _OUR_CREDENTIAL_FAULTS:  # noqa: PLR2004
            self._router.failed(target, now)
        else:
            self._router.succeeded(target, now)  # the deployment answered: the request was bad


def _failure_for(err: UpstreamError, provider: str) -> Failed:
    """The client's view of an answered provider error that did not fail over."""
    code = err.code
    if code is ErrorCode.INVALID_REQUEST:
        return Failed(Refusal(code, "The provider rejected the request."), 400)
    if code is ErrorCode.REQUEST_TOO_LARGE:
        return Failed(Refusal(code, "The request is too large for the model."), 413)
    if code is ErrorCode.UPSTREAM_THROTTLED:  # a 429 the catalog does not treat as free
        return _throttled(provider, err.retry_after_seconds or 1)
    if code is ErrorCode.UPSTREAM_UNAVAILABLE:
        return unavailable(provider, "The provider could not serve the request.", 502)
    return Failed(Refusal(ErrorCode.INTERNAL_ERROR, "The provider rejected the gateway."), 500)
