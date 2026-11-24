"""Budget warnings posted to a webhook through the whole gateway, with a fake receiver.

One signed post per warning the ledger reports; a receiver that is slow, stuck or failing never
delays or fails a call, and a full queue is counted. The receiver is an `httpx.MockTransport`.
"""

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx

from spendgate.config import Secret, WebhookTarget
from spendgate.gateway.alert_webhook import SIGNATURE_HEADER, WebhookAlerts
from spendgate.gateway.metrics import Metrics
from spendgate.pipeline.alerts import BUDGET_LOGGER
from tests.fakes import ABSURD_USAGE, FakeUpstream, Script
from tests.integration.harness import NOW_MS, Stack, async_test, open_stack, wait_until

SIGNING = "fake-webhook-signing-secret"  # pragma: allowlist secret
PROMPT = "please-keep-this-prompt-out-of-webhooks"
type Receive = Callable[[httpx.Request], Awaitable[httpx.Response]]


class Hook:
    """The poster on the budget logger, its metrics and the requests the receiver saw."""

    alerts: WebhookAlerts

    def __init__(self, metrics: Metrics) -> None:
        self.metrics = metrics
        self.seen: list[httpx.Request] = []
        self.waits: list[float] = []  # backoff the poster asked for; nothing really waits

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)

    def count(self, result: str) -> float:
        return self.metrics.alert_webhooks.value(result=result)


@asynccontextmanager
async def hooked(
    s: Stack, answer: Receive, *, queue: int = 100, run: bool = True
) -> AsyncIterator[Hook]:
    hook = Hook(Metrics())

    async def receiver(request: httpx.Request) -> httpx.Response:
        hook.seen.append(request)
        return await answer(request)

    target = WebhookTarget(Secret("https://hooks.example.test/b"), Secret(SIGNING), 5, 3, queue)
    budget = logging.getLogger(BUDGET_LOGGER)
    async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as client:
        alerts = WebhookAlerts(
            target, client, hook.metrics, logging.getLogger("tests.hook"), now=s.clock.now,
            sleep=hook.sleep,
        )  # fmt: skip
        hook.alerts = alerts
        budget.addHandler(alerts)
        poster = asyncio.ensure_future(alerts.run()) if run else None
        try:
            yield hook
        finally:
            budget.removeHandler(alerts)
            if poster is not None:
                poster.cancel()
                await asyncio.gather(poster, return_exceptions=True)


async def ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(204)


async def breach(s: Stack) -> list[int]:
    """One call whose usage breaches the budget (cap_breached + blocked), then a refused one."""
    statuses = []
    for _ in range(2):
        messages = [{"role": "user", "content": PROMPT}]
        body = {"model": "alpha", "max_tokens": 100, "messages": messages}
        statuses.append((await s.post("/v1/chat/completions", body)).status_code)
    return statuses


def absurd() -> FakeUpstream:
    fake = FakeUpstream()
    fake.enqueue(Script(usage=ABSURD_USAGE))
    return fake


def verifies(request: httpx.Request) -> bool:
    digest = hmac.new(SIGNING.encode(), request.content, hashlib.sha256).hexdigest()
    return hmac.compare_digest(request.headers[SIGNATURE_HEADER], f"sha256={digest}")


@async_test
async def test_each_budget_warning_is_posted_once_and_signed(tmp_path: Path) -> None:
    async with open_stack(tmp_path, fake=absurd(), budget="1") as s, hooked(s, ok) as hook:
        assert await breach(s) == [200, 402]  # blocked: the refusal adds no warning
        await wait_until(lambda: hook.count("delivered") == 2)
        bodies = [json.loads(r.content) for r in hook.seen]
    assert len(hook.seen) == 2 and all(verifies(r) for r in hook.seen)
    assert [(b["event"], b["budget_id"]) for b in bodies] == [
        ("cap_breached", s.budget.id), ("blocked", s.budget.id),
    ]  # fmt: skip
    assert len({b["event_id"] for b in bodies}) == 2
    stamp = datetime.fromtimestamp(NOW_MS / 1000, UTC).isoformat(timespec="milliseconds")
    assert all(b["scope_path"] == s.scope.path and b["ts"] == stamp for b in bodies)
    text = b"".join(r.content for r in hook.seen).decode()
    for private in (PROMPT, s.secret, s.secret[:8], "sg_req"):
        assert private not in text


@async_test
async def test_a_stuck_receiver_never_delays_a_call(tmp_path: Path) -> None:
    release = asyncio.Event()

    async def stuck(_request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(200)

    async with open_stack(tmp_path, fake=absurd(), budget="1") as s, hooked(s, stuck) as hook:
        assert await breach(s) == [200, 402]  # answered while the receiver holds the first post
        await wait_until(lambda: len(hook.seen) == 1)
        assert hook.count("delivered") == 0 and hook.alerts.pending() == 1
        spent, held = await s.totals()
        assert held == 0 and spent > 0  # settled as usual
        release.set()
        await wait_until(lambda: hook.count("delivered") == 2)


@async_test
async def test_a_failing_receiver_never_fails_a_call(tmp_path: Path) -> None:
    async def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("receiver down")

    async with open_stack(tmp_path, fake=absurd(), budget="1") as s, hooked(s, down) as hook:
        assert await breach(s) == [200, 402]
        await wait_until(lambda: hook.count("failed") == 2)
        assert s.observer.internal == []
    assert len(hook.seen) == 6 and hook.waits == [1.0, 2.0] * 2  # 3 tries each, with backoff
    assert hook.count("delivered") == 0


@async_test
async def test_a_full_queue_drops_and_counts_without_touching_the_call(tmp_path: Path) -> None:
    async with (
        open_stack(tmp_path, fake=absurd(), budget="1") as s,
        hooked(s, ok, queue=1, run=False) as hook,
    ):
        assert await breach(s) == [200, 402]
        assert hook.alerts.pending() == 1 and hook.count("dropped") == 1
    assert hook.seen == []
