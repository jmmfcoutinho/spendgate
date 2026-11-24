"""Budget warnings posted to the operator's webhook (`alerts.webhook`), off the request path.

`WebhookAlerts` is a second handler on the `spendgate.budget` logger, next to the JSON one: every
warning `pipeline.alerts` logs is also queued here, so the body is the log line's own fields
through the same allowlist (`logs.redact`), plus an event id and a timestamp. Queuing is all that
happens on the request path: it never waits, never raises, and a full queue drops the warning and
counts it. One background task posts the queue in order, signed with HMAC-SHA256, retrying a few
times with backoff. A slow or failing receiver therefore delays only later warnings, never a call;
the ledger stays the record (`soft_alerted_at`, `review_required_at`).
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from datetime import UTC, datetime

import httpx

from spendgate.config import WebhookTarget
from spendgate.core.budgets import AlertKind
from spendgate.gateway.logs import log_event, redact
from spendgate.gateway.metrics import Metrics
from spendgate.ledger.ids import new_id
from spendgate.pipeline.alerts import BUDGET_LOGGER, FIELDS_ATTR
from spendgate.pipeline.model import Clock

SIGNATURE_HEADER = "X-Spendgate-Signature"
RESULTS = ("delivered", "failed", "dropped")
_EVENTS = frozenset(kind.value for kind in AlertKind)
_FIRST_BACKOFF_SECONDS = 1.0  # doubled after each failed attempt, up to the cap
_MAX_BACKOFF_SECONDS = 30.0
_DELIVERED = range(200, 300)
_TOO_MANY_REQUESTS = 429
_SERVER_ERROR = 500
type Sleeper = Callable[[float], Awaitable[None]]


def sign(secret: str, body: bytes) -> str:
    """The signature header value: `sha256=` and the hex HMAC-SHA256 of the exact body bytes."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def event_id() -> str:
    return "sg_evt_" + new_id()


def _on_loop(loop: asyncio.AbstractEventLoop) -> bool:
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


class WebhookAlerts(logging.Handler):
    """Queue budget warnings as signed JSON bodies; `run` posts them until cancelled."""

    def __init__(  # noqa: PLR0913 - the deliverer's collaborators, injected for tests
        self, target: WebhookTarget, client: httpx.AsyncClient, metrics: Metrics,
        logger: logging.Logger, *, now: Clock, sleep: Sleeper = asyncio.sleep,
        new_event_id: Callable[[], str] = event_id,
    ) -> None:  # fmt: skip
        super().__init__(logging.WARNING)
        self._target, self._client, self._metrics, self._logger = target, client, metrics, logger
        self._now, self._sleep, self._new_id = now, sleep, new_event_id
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(target.queue_size)
        self._loop: asyncio.AbstractEventLoop | None = None  # set when `run` starts
        for result in RESULTS:  # every series exists from the start, so a rate alert can see 0
            metrics.alert_webhooks.inc(0, result=result)

    def emit(self, record: logging.LogRecord) -> None:
        """Runs inside the call that logged: build the body and queue it, nothing more."""
        try:
            body = self._body(record)
            if body is None:
                return
            loop = self._loop
            if loop is None or _on_loop(loop):
                self._offer(body)
            else:  # logged from another thread: asyncio.Queue belongs to the loop
                loop.call_soon_threadsafe(self._offer, body)
        except Exception:  # a closed loop, a bad record: the warning is lost, the call is not
            self._metrics.alert_webhooks.inc(result="dropped")

    def _body(self, record: logging.LogRecord) -> bytes | None:
        raw = getattr(record, FIELDS_ATTR, None)
        if not isinstance(raw, Mapping) or raw.get("event") not in _EVENTS:
            return None
        fields = redact(raw)
        fields["event_id"] = self._new_id()
        fields["ts"] = datetime.fromtimestamp(self._now() / 1000, UTC).isoformat(
            timespec="milliseconds"
        )
        return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()

    def _offer(self, body: bytes) -> None:
        try:
            self._queue.put_nowait(body)
        except asyncio.QueueFull:
            self._metrics.alert_webhooks.inc(result="dropped")

    def pending(self) -> int:
        """Warnings queued and not yet taken by the poster."""
        return self._queue.qsize()

    async def run(self) -> None:
        """Post queued warnings one at a time until cancelled; what is left counts as dropped.

        Only a cancel ends it: a warning whose delivery raises counts as failed and the next one
        is posted, so the poster cannot die while the server serves.
        """
        self._loop = asyncio.get_running_loop()
        body: bytes | None = None
        try:
            while True:
                body = await self._queue.get()
                try:
                    await self.deliver(body)
                except Exception as exc:
                    self._metrics.alert_webhooks.inc(result="failed")
                    log_event(
                        self._logger, "alert_webhook_failed", logging.WARNING,
                        reason=type(exc).__name__, http_status=None,
                    )  # fmt: skip
                body = None
        finally:
            unsent = self._queue.qsize() + (body is not None)
            if unsent:
                self._metrics.alert_webhooks.inc(unsent, result="dropped")

    async def deliver(self, body: bytes) -> bool:
        """Up to `max_attempts` posts with backoff; a 2xx is delivered, a 429 or 5xx retried."""
        headers = {
            "content-type": "application/json",
            SIGNATURE_HEADER: sign(self._target.signing_secret.reveal(), body),
        }
        reason, status = "", None
        for attempt in range(self._target.max_attempts):
            if attempt:
                await self._sleep(
                    min(_FIRST_BACKOFF_SECONDS * 2 ** (attempt - 1), _MAX_BACKOFF_SECONDS)
                )
            try:
                status = await self._post(body, headers)
            except Exception as exc:  # any failure is one failed attempt
                reason, status = type(exc).__name__, None  # never the message: it has the URL
                continue
            if status in _DELIVERED:
                self._metrics.alert_webhooks.inc(result="delivered")
                return True
            reason = "status"
            if status != _TOO_MANY_REQUESTS and status < _SERVER_ERROR:
                break  # a redirect or another 4xx will not change on retry
        self._metrics.alert_webhooks.inc(result="failed")
        log_event(
            self._logger, "alert_webhook_failed", logging.WARNING, reason=reason,
            http_status=status,
        )  # fmt: skip
        return False

    async def _post(self, body: bytes, headers: Mapping[str, str]) -> int:
        # A hard bound on the whole attempt (httpx's timeouts are per phase); the reply body is
        # never read, so a receiver cannot make the gateway buffer anything.
        async with asyncio.timeout(self._target.timeout_seconds):
            async with self._client.stream(
                "POST", self._target.url.reveal(), content=body, headers=headers
            ) as reply:
                return reply.status_code


@contextlib.asynccontextmanager
async def webhook_alerts(
    target: WebhookTarget | None, metrics: Metrics, logger: logging.Logger, now: Clock,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[WebhookAlerts | None]:  # fmt: skip
    """The handler attached to `spendgate.budget` for the block, or None when none is set up."""
    if target is None:
        yield None
        return
    # Redirects are not followed: the warning goes to the configured host or nowhere.
    async with httpx.AsyncClient(
        transport=transport, timeout=target.timeout_seconds, follow_redirects=False
    ) as client:
        handler = WebhookAlerts(target, client, metrics, logger, now=now)
        budget = logging.getLogger(BUDGET_LOGGER)
        budget.addHandler(handler)
        try:
            yield handler
        finally:
            budget.removeHandler(handler)


type Job = Callable[[], Coroutine[object, object, None]]


def with_alerts(job: Job, alerts: WebhookAlerts | None) -> Job:
    """One background job that also runs the poster: both stop when the app stops.

    Neither can stop the other: the job is the sweeper and retention (the money backstop), so a
    poster failure must not cancel it, and a job failure must not silence the warnings.
    """
    if alerts is None:
        return job

    async def both() -> None:
        # A cancel reaches both; an exception in one leaves the other running.
        await asyncio.gather(job(), alerts.run(), return_exceptions=True)

    return both
