"""The budget-warning webhook: queue on the request path, post signed bodies in the background.

The receiver is an `httpx.MockTransport`; time and backoff are injected, nothing sleeps.
"""

import asyncio
import hashlib
import hmac
import io
import json
import logging
import threading
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager

import httpx
import pytest

from spendgate.config import Secret, WebhookTarget
from spendgate.core.budgets import AlertKind, BudgetAlert, Period
from spendgate.gateway.alert_webhook import (
    SIGNATURE_HEADER,
    WebhookAlerts,
    sign,
    webhook_alerts,
    with_alerts,
)
from spendgate.gateway.logs import ALLOWED_FIELDS, JsonFormatter
from spendgate.gateway.metrics import Metrics
from spendgate.pipeline.alerts import BUDGET_LOGGER, FIELDS_ATTR, log_alerts

URL = "https://hooks.example.test/budget?token=fake-hook-token"
SIGNING = "fake-signing-secret-for-tests"  # pragma: allowlist secret
NOW_MS = 1_795_305_600_000  # 2026-11-22T00:00:00Z
ALERT = BudgetAlert(AlertKind.SOFT_THRESHOLD, "bud_1", "org:acme", Period.MONTH, NOW_MS, 80, 100)
type Handler = (
    Callable[[httpx.Request], httpx.Response]
    | Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]
)


def target(*, attempts: int = 3, queue: int = 10, signing: str = SIGNING) -> WebhookTarget:
    return WebhookTarget(Secret(URL), Secret(signing), 5, attempts, queue)


class Receiver:
    """A fake webhook receiver: records each request and answers with the next scripted reply."""

    def __init__(self, *answers: int | Exception) -> None:
        self.answers = list(answers) or [204]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer)


class Rig:
    def __init__(
        self, receiver: Handler, *, attempts: int = 3, queue: int = 10, signing: str = SIGNING
    ) -> None:
        self.metrics = Metrics()
        self.log = io.StringIO()
        self.logger = logging.getLogger("tests.alert_webhook")
        stream = logging.StreamHandler(self.log)
        stream.setFormatter(JsonFormatter())
        self.logger.handlers, self.logger.propagate = [stream], False
        self.waits: list[float] = []
        ids = iter(range(1, 1000))
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(receiver))
        hook = target(attempts=attempts, queue=queue, signing=signing)
        self.alerts = WebhookAlerts(
            hook, self.client, self.metrics, self.logger,
            now=lambda: NOW_MS, sleep=self.sleep, new_event_id=lambda: f"sg_evt_{next(ids)}",
        )  # fmt: skip

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)

    def count(self, result: str) -> float:
        return self.metrics.alert_webhooks.value(result=result)

    def emit(self, *alerts: BudgetAlert) -> None:
        log_alerts(alerts, "USD", self.budget_logger())

    def budget_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"{BUDGET_LOGGER}.test")
        logger.handlers, logger.propagate = [self.alerts], False
        return logger


def verifies(request: httpx.Request) -> bool:
    expected = hmac.new(SIGNING.encode(), request.content, hashlib.sha256).hexdigest()
    return hmac.compare_digest(request.headers[SIGNATURE_HEADER], f"sha256={expected}")


async def until(done: Callable[[], bool]) -> None:
    """Yield to the loop until `done` holds; bounded, so a bug fails instead of hanging."""
    for _ in range(10_000):
        if done():
            return
        await asyncio.sleep(0)  # a bare yield to the loop, not a timed wait
    raise AssertionError("condition not reached")


async def post_all(rig: Rig, n: int) -> None:
    """Run the poster until `n` warnings are delivered or given up, then stop it."""
    poster = asyncio.ensure_future(rig.alerts.run())
    await until(lambda: rig.count("delivered") + rig.count("failed") >= n)
    poster.cancel()
    await asyncio.gather(poster, return_exceptions=True)
    await rig.client.aclose()


def test_the_signature_is_hmac_sha256_over_the_exact_body() -> None:
    body = b'{"event":"blocked"}'
    expected = hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert sign("k", body) == f"sha256={expected}"


def test_one_signed_post_per_warning_with_the_log_fields_an_id_and_a_time() -> None:
    receiver = Receiver()
    rig = Rig(receiver)

    async def go() -> None:
        rig.emit(ALERT, BudgetAlert(AlertKind.BLOCKED, "bud_2", "org:acme", Period.NONE, 0, 9, 5))
        await post_all(rig, 2)

    asyncio.run(go())
    assert len(receiver.requests) == 2 and all(verifies(r) for r in receiver.requests)
    first, second = (json.loads(r.content) for r in receiver.requests)
    assert first == {
        "event": "soft_threshold", "logger": BUDGET_LOGGER, "budget_id": "bud_1",
        "scope_path": "org:acme", "period": "month", "period_start": "2026-11-22T00:00:00Z",
        "spent": "0.000000080", "cap": "0.000000100", "currency": "USD",
        "event_id": "sg_evt_1", "ts": "2026-11-22T00:00:00.000+00:00",
    }  # fmt: skip
    assert second["event"] == "blocked" and second["event_id"] == "sg_evt_2"
    assert set(first) - {"event_id"} <= ALLOWED_FIELDS  # the log line's allowlist, nothing more
    request = receiver.requests[0]
    assert str(request.url) == URL and request.method == "POST"
    assert request.headers["content-type"] == "application/json"
    assert rig.count("delivered") == 2 and rig.count("failed") == rig.count("dropped") == 0


def test_only_budget_warnings_are_queued_and_unlisted_fields_never_leave() -> None:
    receiver = Receiver()
    rig = Rig(receiver)
    logger = rig.budget_logger()
    logger.warning("not a budget warning")
    logger.warning("x", extra={FIELDS_ATTR: {"event": "call", "request_id": "sg_req_1"}})
    assert rig.alerts.pending() == 0
    leak = {"event": "blocked", "budget_id": "b", "prompt": "keep-out", "spent": "sk-0123456789ab"}
    logger.warning("x", extra={FIELDS_ATTR: leak})
    asyncio.run(post_all(rig, 1))
    body = json.loads(receiver.requests[0].content)
    assert body["budget_id"] == "b" and "prompt" not in body and "spent" not in body


def test_a_retryable_answer_is_tried_again_with_growing_waits() -> None:
    receiver = Receiver(500, 429, httpx.ConnectError("refused"), 200)
    rig = Rig(receiver, attempts=5)

    async def go() -> bool:
        async with rig.client:
            return await rig.alerts.deliver(b"{}")

    assert asyncio.run(go()) is True
    assert len(receiver.requests) == 4 and rig.waits == [1.0, 2.0, 4.0]
    assert rig.count("delivered") == 1 and rig.count("failed") == 0


def test_backoff_is_capped() -> None:
    rig = Rig(Receiver(503), attempts=8)

    async def go() -> bool:
        async with rig.client:
            return await rig.alerts.deliver(b"{}")

    assert asyncio.run(go()) is False
    assert rig.waits == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


@pytest.mark.parametrize("status", [301, 400, 401, 404])
def test_a_redirect_or_client_error_is_final_and_logged_without_the_url(status: int) -> None:
    receiver = Receiver(status)
    rig = Rig(receiver)

    async def go() -> bool:
        async with rig.client:
            return await rig.alerts.deliver(b"{}")

    assert asyncio.run(go()) is False
    assert len(receiver.requests) == 1 and rig.waits == []
    assert rig.count("failed") == 1
    (line,) = (json.loads(x) for x in rig.log.getvalue().splitlines())
    assert line["event"] == "alert_webhook_failed" and line["http_status"] == status
    assert "hooks.example.test" not in rig.log.getvalue() and "token" not in rig.log.getvalue()


def test_a_receiver_that_keeps_failing_gives_up_after_max_attempts() -> None:
    receiver = Receiver(httpx.ReadTimeout("slow"))
    rig = Rig(receiver, attempts=3)

    async def go() -> bool:
        async with rig.client:
            return await rig.alerts.deliver(b"{}")

    assert asyncio.run(go()) is False
    assert len(receiver.requests) == 3 and rig.count("failed") == 1
    (line,) = (json.loads(x) for x in rig.log.getvalue().splitlines())
    assert line["reason"] == "ReadTimeout" and "hooks.example.test" not in rig.log.getvalue()


def test_a_full_queue_drops_the_new_warning_and_counts_it() -> None:
    rig = Rig(Receiver(), queue=2)
    rig.emit(ALERT, ALERT, ALERT, ALERT, ALERT)
    assert rig.alerts.pending() == 2 and rig.count("dropped") == 3


def test_every_result_series_is_published_at_zero() -> None:
    rendered = Rig(Receiver()).metrics.render()
    for result in ("delivered", "failed", "dropped"):
        assert f'spendgate_alert_webhooks_total{{result="{result}"}} 0' in rendered


def test_queuing_never_waits_for_a_receiver_that_does_not_answer() -> None:
    """A receiver stuck mid-request: the poster waits on it, the logging call never does."""
    stuck = asyncio.Event()
    seen: list[httpx.Request] = []

    async def receiver(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        await stuck.wait()
        return httpx.Response(200)

    rig = Rig(receiver)

    async def go() -> None:
        poster = asyncio.ensure_future(rig.alerts.run())
        rig.emit(ALERT)
        await until(lambda: bool(seen))  # the poster is now stuck in the receiver
        rig.emit(ALERT, ALERT)  # returns at once although the receiver is stuck
        assert rig.alerts.pending() == 2 and rig.count("delivered") == 0
        poster.cancel()
        with pytest.raises(asyncio.CancelledError):
            await poster
        await rig.client.aclose()

    asyncio.run(go())
    assert rig.count("dropped") == 3  # the stuck one and the two queued, when the poster stops


def test_a_warning_logged_from_another_thread_reaches_the_loop() -> None:
    receiver = Receiver()
    rig = Rig(receiver)

    async def go() -> None:
        poster = asyncio.ensure_future(rig.alerts.run())
        await asyncio.sleep(0)  # `run` has recorded its loop
        thread = threading.Thread(target=rig.emit, args=(ALERT,))
        thread.start()
        await asyncio.to_thread(thread.join)
        await until(lambda: rig.count("delivered") == 1)
        poster.cancel()
        await asyncio.gather(poster, return_exceptions=True)
        await rig.client.aclose()

    asyncio.run(go())
    assert len(receiver.requests) == 1 and rig.count("dropped") == 0


def test_a_warning_after_the_loop_closed_is_counted_not_raised() -> None:
    rig = Rig(Receiver())

    async def start() -> None:
        poster = asyncio.ensure_future(rig.alerts.run())
        await asyncio.sleep(0)
        poster.cancel()
        await asyncio.gather(poster, return_exceptions=True)
        await rig.client.aclose()

    asyncio.run(start())  # the loop `run` recorded is now closed
    rig.emit(ALERT)
    assert rig.count("dropped") == 1


def test_a_warning_after_the_poster_stopped_is_counted_as_dropped() -> None:
    """At shutdown the poster stops before the pipeline drains; a settle's warning then is lost."""
    rig = Rig(Receiver())

    async def go() -> None:
        poster = asyncio.ensure_future(rig.alerts.run())
        await asyncio.sleep(0)
        poster.cancel()
        await asyncio.gather(poster, return_exceptions=True)
        rig.emit(ALERT)  # the loop still runs: the queue would take it, but nothing reads it
        await rig.client.aclose()

    asyncio.run(go())
    assert rig.alerts.pending() == 0 and rig.count("dropped") == 1


def test_a_delivery_that_raises_counts_as_failed_and_the_poster_goes_on() -> None:
    """A key that cannot be signed fails each warning, by class only, and never stops `run`."""
    receiver = Receiver()
    rig = Rig(receiver, signing="fake\udcff")  # an undecodable environment byte, escaped

    async def go() -> bool:
        poster = asyncio.ensure_future(rig.alerts.run())
        rig.emit(ALERT, ALERT)
        await until(lambda: rig.count("failed") == 2)
        running = not poster.done()
        poster.cancel()
        await asyncio.gather(poster, return_exceptions=True)
        await rig.client.aclose()
        return running

    assert asyncio.run(go()) is True
    assert receiver.requests == [] and rig.count("delivered") == rig.count("dropped") == 0
    lines = [json.loads(x) for x in rig.log.getvalue().splitlines()]
    assert [(x["event"], x["reason"]) for x in lines] == [
        ("alert_webhook_failed", "UnicodeEncodeError")
    ] * 2
    assert "fake" not in rig.log.getvalue()


@contextmanager
def budget_handlers() -> Iterator[logging.Logger]:
    logger = logging.getLogger(BUDGET_LOGGER)
    saved = list(logger.handlers)
    try:
        yield logger
    finally:
        logger.handlers = saved


def test_the_handler_is_on_the_budget_logger_only_while_open() -> None:
    receiver = Receiver()
    metrics = Metrics()

    async def go() -> None:
        with budget_handlers() as budget:
            async with webhook_alerts(None, metrics, logging.getLogger("t"), lambda: 0) as none:
                assert none is None
            transport = httpx.MockTransport(receiver)
            hook = target()
            async with webhook_alerts(hook, metrics, budget, lambda: NOW_MS, transport) as alerts:
                assert alerts is not None and alerts in budget.handlers
                log_alerts([ALERT], "USD")
                assert alerts.pending() == 1
                poster = asyncio.ensure_future(alerts.run())
                await until(lambda: len(receiver.requests) == 1)
                poster.cancel()
                await asyncio.gather(poster, return_exceptions=True)
            assert alerts not in budget.handlers

    asyncio.run(go())
    assert len(receiver.requests) == 1 and verifies(receiver.requests[0])


def test_with_alerts_runs_the_poster_beside_the_job_and_stops_both() -> None:
    ran: list[str] = []

    async def job() -> None:
        ran.append("job")
        await asyncio.Event().wait()

    assert with_alerts(job, None) is job
    rig = Rig(Receiver())

    async def go() -> None:
        both = asyncio.ensure_future(with_alerts(job, rig.alerts)())
        rig.emit(ALERT)
        await until(lambda: rig.count("delivered") == 1)
        both.cancel()
        await asyncio.gather(both, return_exceptions=True)
        await rig.client.aclose()

    asyncio.run(go())
    assert ran == ["job"] and rig.count("delivered") == 1


class _Crashing(WebhookAlerts):
    async def run(self) -> None:
        raise RuntimeError("poster bug")


def test_a_raising_poster_leaves_the_job_beside_it_running() -> None:
    """The job is the sweeper and retention: a poster failure must not cancel it."""
    rig = Rig(Receiver())
    crashing = _Crashing(target(), rig.client, rig.metrics, rig.logger, now=lambda: NOW_MS)
    release, ticks = asyncio.Event(), [0]

    async def job() -> None:
        while not release.is_set():
            ticks[0] += 1
            await asyncio.sleep(0)

    async def go() -> tuple[bool, bool]:
        both = asyncio.ensure_future(with_alerts(job, crashing)())
        await until(lambda: ticks[0] > 100)  # long after the poster raised
        seen = ticks[0]
        await until(lambda: ticks[0] > seen)
        still = not both.done()
        release.set()
        await until(both.done)
        await rig.client.aclose()
        return still, both.exception() is None

    assert asyncio.run(go()) == (True, True)
    (line,) = (json.loads(x) for x in rig.log.getvalue().splitlines())
    assert (line["event"], line["level"], line["job"], line["reason"]) == (
        "background_job_failed",
        "ERROR",
        "alert_webhook",
        "RuntimeError",
    )
    assert "poster bug" not in rig.log.getvalue()  # the class only, never the message


def test_a_raising_job_leaves_the_poster_beside_it_running() -> None:
    rig = Rig(Receiver())
    crashed: list[bool] = []

    async def job() -> None:
        crashed.append(True)
        raise RuntimeError("sweeper bug")

    async def go() -> None:
        both = asyncio.ensure_future(with_alerts(job, rig.alerts)())
        await until(lambda: bool(crashed))
        for _ in range(10):  # let a cancel caused by the crash land, if there is one
            await asyncio.sleep(0)
        rig.emit(ALERT)  # queued only after the job is gone
        await until(lambda: rig.count("delivered") == 1)
        assert not both.done()
        both.cancel()
        await asyncio.gather(both, return_exceptions=True)
        await rig.client.aclose()

    asyncio.run(go())
    assert rig.count("delivered") == 1
    (line,) = (json.loads(x) for x in rig.log.getvalue().splitlines())
    assert (line["event"], line["job"], line["reason"]) == (
        "background_job_failed",
        "sweeper",
        "RuntimeError",
    )
    assert "sweeper bug" not in rig.log.getvalue()
