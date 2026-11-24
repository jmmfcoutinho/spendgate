"""The provider contract every adapter implements, plus the small shared types it needs.

Pure data and protocols: no I/O, no HTTP client. The pipeline owns sending; an adapter validates,
readies its credentials, translates, converts answers back, relays streams, reads usage and maps
errors. The pipeline never branches on `Provider.name` and calls nothing but this protocol.

`translate` is synchronous by contract, so the one blocking step an adapter may need (a token
refresh) lives in the async `prepare`, which the pipeline awaits just before each `translate`.
"""

import email.utils
import functools
import json
import math
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, fields
from datetime import UTC
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from spendgate.core.cost import Usage
from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.outcomes import Attempt, Outcome, not_processed, outcome_for_status

# Parsed JSON. A recursive alias keeps `Any` out of request and response bodies.
type Json = bool | int | float | str | list[Json] | Mapping[str, Json] | None


# What hostile provider input can raise: deep nesting, bad bytes or numbers, wrong shapes.
UNREADABLE = (RecursionError, ValueError, OverflowError, ArithmeticError, TypeError)

INT64_MAX = 2**63 - 1  # the ledger stores counters as int64: a larger one is not a usage figure
_DELTA = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_MAX_RETRY_SECONDS = 86_400  # a longer hint is junk or an attack; the cap keeps it harmless


def load_json(raw: str | bytes) -> Json:
    """Parse JSON from a provider; None when unreadable, whatever the reason. Never raises."""
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except UNREADABLE:  # includes undecodable bytes and RecursionError from deep nesting
        return None


def count_of(value: Json) -> int | None:
    """A usage counter: a non-negative int that fits int64, else None (unreadable)."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= INT64_MAX:
        return None
    return value


def bounded(usage: Usage | None) -> Usage | None:
    """`usage`, or None when any counter (or a sum an adapter built) does not fit int64."""
    if usage is None:
        return None
    for f in fields(usage):
        value = getattr(usage, f.name)
        if isinstance(value, Decimal):
            if not value.is_finite() or value > INT64_MAX:
                return None
        elif value > INT64_MAX:
            return None
    return usage


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return next((v for k, v in headers.items() if k.lower() == name), None)


def retry_after_seconds(headers: Mapping[str, str]) -> int | None:
    """`Retry-After` as whole seconds (rounded up, capped at a day); None for anything else.

    Delta-seconds only (a decimal fraction is rounded up). An HTTP-date needs the clock, so it is
    None here and the pipeline reads it with `retry_after_date_ms` against its injected clock. A
    sign, a non-ASCII digit (`isdigit` accepts "²", `int` rejects it) or other garbage is also
    None here; the pipeline then sees the header present but unreadable (`has_retry_after`) and
    cools for its full `cooldown_seconds`. Never raises.
    """
    raw = _header(headers, "retry-after")
    if raw is None:
        return None
    text = raw.strip()
    if _DELTA.fullmatch(text) is None:  # ASCII only: `isdigit` would accept "²" and `int` raise
        return None
    whole, _, fraction = text.partition(".")
    if len(whole) > len(str(_MAX_RETRY_SECONDS)):  # also keeps `int` clear of its digit limit
        return _MAX_RETRY_SECONDS
    seconds = int(whole) + (1 if fraction.strip("0") else 0)  # round up: never retry early
    return min(seconds, _MAX_RETRY_SECONDS)


def has_retry_after(headers: Mapping[str, str]) -> bool:
    """Whether the answer carries any retry hint header, readable or not."""
    return any(k.lower() in {"retry-after", "retry-after-ms"} for k in headers)


def retry_after_date_ms(headers: Mapping[str, str]) -> int | None:
    """An HTTP-date `Retry-After` (RFC 9110 10.2.3) as epoch milliseconds; None otherwise.

    Any of the three date forms `email.utils` reads; a zone-less date is taken as UTC (the RFC
    requires GMT). Never raises.
    """
    raw = _header(headers, "retry-after")
    if raw is None or _DELTA.fullmatch(raw.strip()) is not None:
        return None
    try:
        when = email.utils.parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    try:
        return int(when.timestamp() * 1000)
    except (OverflowError, OSError, ValueError):
        return None


def retry_after_ms_seconds(headers: Mapping[str, str]) -> int | None:
    """Azure's `retry-after-ms` in whole seconds, rounded up (never early); never raises."""
    raw = _header(headers, "retry-after-ms")
    if raw is None:
        return None
    try:
        millis = float(raw)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(millis) or millis < 0:
        return None
    return min(math.ceil(millis / 1000), _MAX_RETRY_SECONDS)


class AuthMode(StrEnum):
    """How the gateway authenticates to the provider (credentials never leave the gateway)."""

    API_KEY = "api_key"  # static key in a header  # pragma: allowlist secret
    # Entra ID or Google service-account token (Vertex).
    BEARER_TOKEN = "bearer_token"  # noqa: S105 - a mode name, not a secret  # pragma: allowlist secret
    SIGV4 = "sigv4"  # AWS request signing, credentials from the standard chain


@dataclass(frozen=True, slots=True)
class Deployment:
    """One routable target from config: a provider endpoint serving one model."""

    provider: str  # provider slug == adapter module name
    base_url: str  # from config, never from the client
    model: str  # the provider's model or deployment name
    auth: AuthMode = AuthMode.API_KEY
    region: str | None = None
    api_version: str | None = None  # Azure OpenAI query version; None for the v1 path
    credential: str = field(default="", repr=False)  # secret or token; kept out of reprs and logs


@dataclass(frozen=True, slots=True)
class CleanRequest:
    """A validated client request (allowlist already applied), before provider translation."""

    endpoint: str  # protocol path, e.g. "/v1/chat/completions"
    model: str  # gateway model name the client asked for
    body: Mapping[str, Json] = field(repr=False)  # user content; only allowlisted fields remain
    stream: bool = False
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)  # client headers
    files: Mapping[str, bytes] = field(default_factory=dict, repr=False)  # multipart parts
    # Set by the pipeline (request id, its clock) so converted answers are deterministic in tests;
    # only adapters that convert shapes read them (`response`, `relay`).
    response_id: str = ""
    created: int = 0  # Unix seconds
    # The output-token ceiling the hold was sized with (set by the pipeline from the estimate).
    # Adapters forward exactly this value in one field and drop the client's ceiling fields, so a
    # second, larger field can never reach the provider (M-15). None on endpoints without one.
    output_ceiling: int | None = None


@dataclass(frozen=True, slots=True)
class UpstreamRequest:
    """What the pipeline sends: plain parts, so adapters need no HTTP library."""

    method: str
    url: str
    headers: Mapping[str, str] = field(repr=False)  # carries provider credentials
    body: bytes = field(repr=False)  # may contain user content
    stream: bool = False


@dataclass(frozen=True, slots=True)
class UpstreamResponse:
    """A complete (non-streamed) provider answer, or the head plus body of an error."""

    status: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class SseEvent:
    """One parsed event: named (`event:` line) or not, with its joined `data:` lines.

    Binary framings (Bedrock event-stream) are decoded by the adapter into the same shape, with
    the frame's event type as `event`.
    """

    event: str | None
    data: str


@dataclass(frozen=True, slots=True)
class SseLimits:
    """Stream limits (threat-model C-22), enforced by every `StreamRelay`.

    The defaults are placeholders: the docs name the limits but not the numbers, so 5.3 sets them
    and passes them to `Provider.relay`.
    """

    max_line_bytes: int = 64 * 1024
    max_event_bytes: int = 1024 * 1024
    max_events: int = 100_000
    max_stream_bytes: int = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class UpstreamError:
    """A provider failure mapped to core terms.

    `sent` is False for failures before the request left (connect, DNS, TLS); `status` is the
    provider's HTTP status when one came back. Whether the attempt is free depends on the
    catalog's `free_statuses`, so it is a method, not a stored flag.
    """

    code: ErrorCode
    sent: bool = True
    status: int | None = None
    retry_after_seconds: int | None = None
    message: str = field(default="", repr=False)  # provider text; never shown to the client

    def not_processed(self, free_statuses: Collection[int]) -> bool:
        return not_processed(Attempt(self.sent, self.status), free_statuses)

    def outcome(self, free_statuses: Collection[int]) -> Outcome:
        """Settlement outcome for an answered error; a never-sent failure is `unreachable`."""
        if not self.sent:
            return Outcome.UNREACHABLE
        if self.status is None:
            raise ValueError("a sent attempt without a status has no answered outcome")
        return outcome_for_status(self.status, free_statuses)


class Untranslatable(Exception):
    """A request the adapter cannot accept; the pipeline answers with `refusal` (invalid_request).

    Raised by `validate` helpers and by `translate`. The message names the field, never its value.
    """

    def __init__(self, message: str, param: str | None = None) -> None:
        super().__init__(message)
        self.refusal = Refusal(ErrorCode.INVALID_REQUEST, message, param)


def held_ceiling(req: CleanRequest, param: str) -> int:
    """The ceiling to forward; a ceiling endpoint without one is refused, never sent unbounded."""
    ceiling = req.output_ceiling
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling <= 0:
        raise Untranslatable("An output token ceiling is required.", param)
    return ceiling


class StreamBroken(Exception):
    """The upstream stream is malformed (framing, CRC, JSON): settled as `stream_broken`."""


class StreamLimitExceeded(StreamBroken):
    """A `SseLimits` bound was crossed (threat-model C-22): also settled as `stream_broken`."""


class StreamRelay(Protocol):
    """One streamed request: upstream bytes in, client bytes out, usage on the side.

    Created per request by `Provider.relay`. Raises `StreamBroken` (or its subclass
    `StreamLimitExceeded`) on malformed or oversized input; `usage()` still answers afterwards.
    """

    def feed(self, chunk: bytes) -> bytes:
        """Bytes to send to the client for this upstream chunk (possibly empty)."""
        ...

    def close(self) -> bytes:
        """End of the upstream body: trailing bytes (e.g. `[DONE]`); rejects a half event."""
        ...

    def usage(self) -> Usage | None:
        """The answer's FINAL usage, normalised, or None (the pipeline then charges the hold).

        Final means end-of-stream usage (OpenAI's usage-only chunk or a terminal Responses event,
        Anthropic's `message_delta`, Bedrock's `metadata`, Gemini's totals sent with the finish
        reason). A running total from mid-stream is never returned: a stream cut, cancelled or
        stopped before its final usage arrived is charged the full hold (M-13).
        """
        ...


class Provider(Protocol):
    name: str  # provider slug, equal to the adapter module name

    def validate(self, req: CleanRequest) -> Refusal | None:
        """Pure endpoint, body, header and file checks, once before routing (one provider serves
        every candidate of a catalog id). Adapter configuration is given to the constructor."""
        ...

    async def prepare(self, deployment: Deployment) -> None:
        """Ready the deployment's credentials before `translate`, without blocking the loop.

        A token refresh (Entra ID, google-auth, the AWS chain) is a blocking network call: an
        adapter that needs one runs it here in a worker thread, one refresh at a time per
        credential (`providers.refresh`), so `translate` then finds a cached token. Static-key
        adapters do nothing. A credential fault raises, as it would from `translate`: the
        pipeline answers internal_error and nothing is sent. `translate` then signs with what
        `prepare` readied and never refreshes on the loop; without a `prepare` (direct callers,
        tests) it fetches inline.
        """
        ...

    def translate(self, req: CleanRequest, deployment: Deployment) -> UpstreamRequest:
        """Shape, URL (config base plus a fixed path) and auth for one routed candidate.

        Raises `Untranslatable` for a request the chosen provider cannot carry.
        """
        ...

    def response(self, req: CleanRequest, resp: UpstreamResponse) -> UpstreamResponse:
        """A non-streamed answer in the client's shape: identity unless the adapter converts.

        Converting adapters take the id, time and model from `req` (`response_id`, `created`,
        `model`); an unreadable 2xx becomes a bodiless 502 and the pipeline charges the hold.
        """
        ...

    def relay(self, req: CleanRequest, limits: SseLimits = SseLimits()) -> StreamRelay:  # noqa: B008
        """A fresh relay for one streamed request, enforcing `limits` (frozen, shared default)."""
        ...

    def usage(self, req: CleanRequest, response: UpstreamResponse) -> Usage | None:
        """Usage of a non-streamed answer; None when absent or unreadable.

        Request-priced calls (OpenAI speech: characters) are read from `req`; others ignore it.
        """
        ...

    def error(self, response: UpstreamResponse) -> UpstreamError:
        """Map an error answer to a core error code."""
        ...


# --- adapters never raise after a send ----------------------------------------------------------
# A raise from `usage`/`error`/`response` would lose the attempt and release the hold at cost 0.
# These make "unreadable" the answer instead: usage None (the hold stands), a mapped 5xx-like
# error, a bodiless 502.


def never_raises_usage[S](
    fn: Callable[[S, CleanRequest, UpstreamResponse], Usage | None],
) -> Callable[[S, CleanRequest, UpstreamResponse], Usage | None]:
    @functools.wraps(fn)
    def run(self: S, req: CleanRequest, resp: UpstreamResponse) -> Usage | None:
        try:
            return bounded(fn(self, req, resp))
        except UNREADABLE:
            return None

    return run


def never_raises_response[S](
    fn: Callable[[S, CleanRequest, UpstreamResponse], UpstreamResponse],
) -> Callable[[S, CleanRequest, UpstreamResponse], UpstreamResponse]:
    @functools.wraps(fn)
    def run(self: S, req: CleanRequest, resp: UpstreamResponse) -> UpstreamResponse:
        try:
            return fn(self, req, resp)
        except UNREADABLE:
            return UpstreamResponse(502, {}, b"")  # unreadable: the pipeline charges the hold

    return run


def never_raises_error[S](
    fn: Callable[[S, UpstreamResponse], UpstreamError],
) -> Callable[[S, UpstreamResponse], UpstreamError]:
    @functools.wraps(fn)
    def run(self: S, resp: UpstreamResponse) -> UpstreamError:
        try:
            return fn(self, resp)
        except UNREADABLE:
            return UpstreamError(ErrorCode.UPSTREAM_UNAVAILABLE, True, resp.status)

    return run
