"""Provider throttling (issue #36): a short jittered cooldown, Retry-After, and a bounded wait.

A 429 without Retry-After cools its deployment for 250 to 500 ms only. A call that finds every
untried deployment cooling waits for the soonest one (at most `routing.MAX_WAIT_MS`, inside the
request deadline) instead of refusing at once. The wait happens inside the call's one hold and
sends nothing, so it never changes what the call costs. `Sleeps` stands in for `asyncio.sleep`
and moves the stack's fake clock: no test really sleeps.
"""

import asyncio
import email.utils
from pathlib import Path

import httpx

from spendgate.core.health import HealthConfig
from spendgate.core.outcomes import Outcome, ReservationState
from spendgate.pipeline.routing import MAX_WAIT_MS
from tests.conftest import FakeClock
from tests.fakes import Script
from tests.integration.harness import NOW_MS, Stack, async_test, open_stack

Alpha_CALL = 3500  # nano USD: 10 input and 5 output tokens at the catalog price
SHORT = HealthConfig()


class Sleeps:
    """A fake `asyncio.sleep` that records each pause and advances the stack's clock by it."""

    def __init__(self) -> None:
        self.taken: list[float] = []
        self.clock: FakeClock | None = None

    async def __call__(self, seconds: float) -> None:
        assert self.clock is not None, "bind the stack's clock first"
        self.taken.append(seconds)
        self.clock.advance(ms=round(seconds * 1000))
        await asyncio.sleep(0)


def hosts(s: Stack) -> list[str]:
    return [r.headers["host"].split(".")[0] for r in s.fake.requests]


def rid_of(response: httpx.Response) -> str:
    return response.headers["x-spendgate-request-id"]


def code_of(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


@async_test
async def test_a_429_without_retry_after_cools_for_a_short_jittered_interval(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(status=429))
        r = await s.chat("alpha-one")
        assert r.status_code == 429 and code_of(r) == "upstream_throttled"
        cooled = s.observer.attempts[0].cooldown_ms
        assert cooled is not None
        assert SHORT.throttle_min_ms <= cooled <= SHORT.throttle_max_ms  # not the 5 s default
        assert r.headers["retry-after"] == "1"  # whole seconds, rounded up


@async_test
async def test_m01_a_call_waits_for_a_cooling_deployment_inside_its_one_hold(
    tmp_path: Path,
) -> None:
    """Every deployment cooling: the next call waits, then is served on one hold, charged once."""
    sleeps = Sleeps()
    async with open_stack(tmp_path, pipeline_kwargs={"sleep": sleeps}) as s:
        sleeps.clock = s.clock
        s.fake.enqueue(Script(status=429), Script(status=429))
        first = await s.chat()
        assert first.status_code == 429 and hosts(s) == ["a", "b"] and sleeps.taken == []
        second = await s.chat()  # a and b are both cooling: wait for the soonest, then go
        assert second.status_code == 200, second.text
        soonest = min(a.cooldown_ms or 0 for a in s.observer.attempts[:2])
        assert len(sleeps.taken) == 1  # the soonest plus this waiter's own jitter
        assert soonest <= sleeps.taken[0] * 1000 <= soonest + SHORT.throttle_min_ms
        assert len(hosts(s)) == 3
        calls = await s.ledger.get_calls(rid_of(second))
        assert [(c.attempt, c.outcome, c.charged_nano) for c in calls] == [
            (1, Outcome.OK, Alpha_CALL)
        ]
        assert len(s.observer.held) == 2  # one hold per call: the wait reserved nothing more
        assert await s.totals() == (Alpha_CALL, 0)


@async_test
async def test_m13_a_wait_then_a_free_failover_still_charges_only_the_answer(
    tmp_path: Path,
) -> None:
    """After the wait the first deployment throttles again: free failover to the other one."""
    sleeps = Sleeps()
    async with open_stack(tmp_path, pipeline_kwargs={"sleep": sleeps}) as s:
        sleeps.clock = s.clock
        s.fake.enqueue(
            Script(status=429, retry_after="1"), Script(status=429, retry_after="1"),
            Script(status=429),
        )  # fmt: skip
        assert (await s.chat()).status_code == 429  # a and b now cool for exactly 1 s
        r = await s.chat()  # waits 1 s for a, a throttles again, then b (back at once) answers
        assert r.status_code == 200, r.text
        assert sleeps.taken == [1.0]
        assert hosts(s) == ["a", "b", "a", "b"]
        calls = await s.ledger.get_calls(rid_of(r))
        assert [(c.attempt, c.outcome, c.charged_nano) for c in calls] == [
            (1, Outcome.THROTTLED, 0),
            (2, Outcome.OK, Alpha_CALL),
        ]
        assert await s.totals() == (Alpha_CALL, 0)


@async_test
async def test_a_retry_after_beyond_the_wait_bound_is_refused_at_once(tmp_path: Path) -> None:
    sleeps = Sleeps()
    async with open_stack(tmp_path, pipeline_kwargs={"sleep": sleeps}) as s:
        sleeps.clock = s.clock
        s.fake.enqueue(Script(status=429, retry_after="2"))
        await s.chat("alpha-one")
        assert s.observer.attempts[0].cooldown_ms == 2_000 > MAX_WAIT_MS  # honoured as given
        r = await s.chat("alpha-one")
        assert r.status_code == 429 and code_of(r) == "upstream_throttled"
        assert sleeps.taken == [] and s.fake.calls == 1  # no wait, no contact
        assert r.headers["retry-after"] == "2"
        row = await s.ledger.get_reservation(rid_of(r))
        assert row is not None and row.state is ReservationState.RELEASED


@async_test
async def test_the_wait_leaves_a_full_wait_bound_before_the_deadline(tmp_path: Path) -> None:
    """A 1 s cooldown fits the wait bound, but waiting it out of a 1.5 s deadline would leave
    the call 0.5 s to be sent and maybe charged in full: refused free instead."""
    sleeps = Sleeps()
    kwargs: dict[str, object] = {"sleep": sleeps, "max_request_seconds": 1.5}
    async with open_stack(tmp_path, pipeline_kwargs=kwargs) as s:
        sleeps.clock = s.clock
        s.fake.enqueue(Script(status=429, retry_after="1"))
        await s.chat("alpha-one")
        r = await s.chat("alpha-one")
        assert r.status_code == 429 and sleeps.taken == [] and s.fake.calls == 1
        assert await s.totals() == (0, 0)


@async_test
async def test_a_cooldown_of_exactly_the_wait_bound_is_waited_for(tmp_path: Path) -> None:
    sleeps = Sleeps()
    async with open_stack(tmp_path, pipeline_kwargs={"sleep": sleeps}) as s:
        sleeps.clock = s.clock
        s.fake.enqueue(Script(status=429, retry_after="1"))
        await s.chat("alpha-one")
        r = await s.chat("alpha-one")
        assert r.status_code == 200 and sleeps.taken == [MAX_WAIT_MS / 1000]
        assert await s.totals() == (Alpha_CALL, 0)


@async_test
async def test_a_wait_that_ends_with_nothing_back_is_refused_free(tmp_path: Path) -> None:
    """The clock stands still while it waits: the wait bound still ends it, never a spin."""
    taken: list[float] = []

    async def frozen(seconds: float) -> None:
        taken.append(seconds)
        await asyncio.sleep(0)

    async with open_stack(tmp_path, pipeline_kwargs={"sleep": frozen}) as s:
        s.fake.enqueue(Script(status=429))
        await s.chat("alpha-one")
        r = await s.chat("alpha-one")
        assert r.status_code == 429 and code_of(r) == "upstream_throttled"
        assert len(taken) >= 1 and sum(taken) * 1000 <= MAX_WAIT_MS
        assert s.fake.calls == 1 and await s.totals() == (0, 0)


def http_date(ms: int) -> str:
    return email.utils.formatdate(ms / 1000, usegmt=True)


@async_test
async def test_an_http_date_retry_after_is_read_against_the_injected_clock(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(status=429, retry_after=http_date(NOW_MS + 3_000)))
        r = await s.chat("alpha-one")
        assert [a.cooldown_ms for a in s.observer.attempts] == [3_000]
        assert r.status_code == 429 and r.headers["retry-after"] == "3"


@async_test
async def test_a_past_http_date_retry_after_gets_the_short_cooldown(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(status=429, retry_after=http_date(NOW_MS - 60_000)))
        await s.chat("alpha-one")
        cooled = s.observer.attempts[0].cooldown_ms
        assert cooled is not None
        assert SHORT.throttle_min_ms <= cooled <= SHORT.throttle_max_ms


@async_test
async def test_a_garbage_retry_after_cools_for_the_full_cooldown(tmp_path: Path) -> None:
    """Nobody knows what an unreadable hint asked for: the breaker cooldown, not the short one."""
    async with open_stack(tmp_path, cooldown=5) as s:
        s.fake.enqueue(Script(status=429, retry_after="in a little while"))
        r = await s.chat("alpha-one")
        assert [a.cooldown_ms for a in s.observer.attempts] == [5_000]
        assert r.status_code == 429 and r.headers["retry-after"] == "5"


@async_test
async def test_a_valid_retry_after_ms_on_an_openai_429_is_honoured(tmp_path: Path) -> None:
    """The OpenAI adapter reads only Retry-After; Azure's `retry-after-ms` is still a hint."""
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(status=429, headers=(("retry-after-ms", "200"),)))
        r = await s.chat("alpha-one")
        # 200 ms rounds up to 1 s, which beats the short draw; not the 5 s for an unreadable hint
        assert [a.cooldown_ms for a in s.observer.attempts] == [1_000]
        assert r.status_code == 429 and r.headers["retry-after"] == "1"


class GatedSleep:
    """A sleep that holds the waiting call until the test releases it (or cancels the call)."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.entered.set()
        await self.release.wait()


async def _cancel_mid_wait(tmp_path: Path, *, stream: bool) -> None:
    sleeps = GatedSleep()
    async with open_stack(tmp_path, pipeline_kwargs={"sleep": sleeps}) as s:
        s.fake.enqueue(Script(status=429))
        assert (await s.chat("alpha-one")).status_code == 429
        extra: dict[str, object] = {"stream": True} if stream else {}
        call = asyncio.ensure_future(s.chat("alpha-one", **extra))
        await asyncio.wait_for(sleeps.entered.wait(), 5)  # held, now waiting for the cooldown
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        await s.pipeline.drain()  # the release runs in a task a cancel cannot interrupt
        assert s.fake.calls == 1  # nothing was sent while it waited
        row = await s.ledger.get_reservation(s.observer.held[-1].request_id)
        assert row is not None and row.state is ReservationState.RELEASED
        assert row.charged_nano == 0 and await s.totals() == (0, 0)


@async_test
async def test_m13_a_call_cancelled_while_waiting_is_released_at_zero(tmp_path: Path) -> None:
    await _cancel_mid_wait(tmp_path, stream=False)


@async_test
async def test_m13_a_stream_cancelled_while_waiting_is_released_at_zero(tmp_path: Path) -> None:
    await _cancel_mid_wait(tmp_path, stream=True)
