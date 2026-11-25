"""What the failover loop (`pipeline.routing.Forwarder`) returns, and how a failed request reads.

Pure values and decisions, no I/O: the result types, and the refusal the client gets when a
request ends without an answer (every candidate tried or cooling, or an error that ends it).
"""

from dataclasses import dataclass

from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.outcomes import Outcome
from spendgate.pipeline.candidates import Cause, Router, Target
from spendgate.pipeline.faults import Fault
from spendgate.pipeline.model import Clock
from spendgate.pipeline.settle import Book
from spendgate.providers.port import CleanRequest, Provider, UpstreamError, UpstreamResponse


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
    fault: Fault | None = None  # a catch-all's redacted trace, reported once the call is closed


@dataclass(frozen=True, slots=True)
class Retry:
    """An attempt that was `not_processed`: the loop may fail over."""

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


def throttled(provider: str, retry_after_s: int) -> Failed:
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


def exhausted(router: Router, now: Clock, job: Job, book: Book, retries: list[Retry]) -> Failed:
    """Every attempt was not_processed (or none could be made): the hold is released."""
    provider = job.targets[0].provider
    hinted = [r.retry_after_s for r in retries if r.retry_after_s is not None]
    wait = max(hinted) if hinted else router.wait_seconds(job.targets, now())
    if retries:
        if all(r.cause is Cause.QUOTA for r in retries):  # retrying a billing fault is futile
            return unavailable(provider, "The provider could not serve the request.", 502)
        if any(r.cause in (Cause.THROTTLED, Cause.QUOTA) for r in retries):
            return throttled(provider, wait)
        return unavailable(provider, "The provider could not be reached.", 502, retry=True)
    # Nothing was tried: every deployment is cooling or circuit-open.
    if router.all_quota(job.targets):
        book.idle_outcome = Outcome.THROTTLED
        return unavailable(provider, "The provider could not serve the request.", 502)
    if router.all_throttled(job.targets):
        book.idle_outcome = Outcome.THROTTLED
        return throttled(provider, wait)
    book.idle_outcome = Outcome.UNREACHABLE
    refusal = Refusal(
        ErrorCode.UPSTREAM_UNAVAILABLE,
        "Every deployment is temporarily unavailable.",
        details={"provider": provider},
        retry_after_seconds=wait,
    )
    return Failed(refusal, 503, should_retry=True)


def failure_for(err: UpstreamError, provider: str) -> Failed:
    """The client's view of an answered provider error that did not fail over."""
    code = err.code
    if code is ErrorCode.INVALID_REQUEST:
        return Failed(Refusal(code, "The provider rejected the request."), 400)
    if code is ErrorCode.REQUEST_TOO_LARGE:
        return Failed(Refusal(code, "The request is too large for the model."), 413)
    if code is ErrorCode.UPSTREAM_THROTTLED:  # a 429 the catalog does not treat as free
        return throttled(provider, err.retry_after_seconds or 1)
    if code is ErrorCode.UPSTREAM_UNAVAILABLE:
        return unavailable(provider, "The provider could not serve the request.", 502)
    return Failed(Refusal(ErrorCode.INTERNAL_ERROR, "The provider rejected the gateway."), 500)
