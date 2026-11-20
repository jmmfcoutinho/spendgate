"""Routing and provider-throttle handling through the whole stack.

Alias `alpha` has two deployments, hosts `a.` and `b.` (config order). The fake upstream's request
log shows which host each request reached; the ledger shows what each attempt cost.
"""

from pathlib import Path

import httpx

from spendgate.core.outcomes import Outcome, ReservationState
from tests.fakes import Script
from tests.integration.harness import RaisingTransport, Stack, async_test, open_stack

Alpha_CALL = 3500  # nano USD: 10 input and 5 output tokens at the catalog price


def hosts(s: Stack) -> list[str]:
    return [r.headers["host"].split(".")[0] for r in s.fake.requests]


def rid_of(response: httpx.Response) -> str:
    return response.headers["x-spendgate-request-id"]


def code_of(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


@async_test
async def test_throttle_fails_over_inside_one_reservation(tmp_path: Path) -> None:
    """T-MON-02: A answers 429 Retry-After 3, the call goes to B, A's attempt costs 0."""
    async with open_stack(tmp_path, cooldown=1) as s:
        s.fake.enqueue(Script(status=429, retry_after="3"))
        r = await s.chat()
        assert r.status_code == 200 and hosts(s) == ["a", "b"]
        calls = await s.ledger.get_calls(rid_of(r))
        assert [(c.attempt, c.outcome, c.deployment) for c in calls] == [
            (1, Outcome.THROTTLED, "alpha/0"),
            (2, Outcome.OK, "alpha/1"),
        ]
        assert [c.charged_nano for c in calls] == [0, Alpha_CALL]
        assert await s.totals() == (Alpha_CALL, 0)  # one hold, one charge across both attempts
        assert len(s.observer.held) == 1
        assert [a.cooldown_ms for a in s.observer.attempts] == [3000, None]


@async_test
async def test_unreachable_deployment_fails_over_at_zero_cost(tmp_path: Path) -> None:
    """M-13: A refuses the connection before send; B answers; A's attempt is free."""
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(refuse_connection=True))
        r = await s.chat()
        assert r.status_code == 200 and hosts(s) == ["b"]  # the refused request is never logged
        calls = await s.ledger.get_calls(rid_of(r))
        assert [(c.outcome, c.charged_nano) for c in calls] == [
            (Outcome.UNREACHABLE, 0),
            (Outcome.OK, Alpha_CALL),
        ]


@async_test
async def test_server_error_is_not_failed_over_and_is_charged_in_full(tmp_path: Path) -> None:
    """A 5xx after send ends the request at the full hold, x-should-retry false."""
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(status=500))
        r = await s.chat()
        assert r.status_code == 502 and code_of(r) == "upstream_unavailable"
        assert r.headers["x-should-retry"] == "false"
        assert hosts(s) == ["a"]
        held = s.observer.held[0].hold.nano
        assert await s.totals() == (held, 0)
        assert r.headers["x-spendgate-cost"].startswith("0.00")
        calls = await s.ledger.get_calls(rid_of(r))
        assert [(c.outcome, c.charged_nano) for c in calls] == [(Outcome.UPSTREAM_5XX, held)]


@async_test
async def test_provider_rejection_is_free_and_not_failed_over(tmp_path: Path) -> None:
    """A provider 4xx without usage settles at 0 and is not retried elsewhere (M-13)."""
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(status=400))
        r = await s.chat()
        assert r.status_code == 400 and code_of(r) == "invalid_request"
        assert hosts(s) == ["a"] and await s.totals() == (0, 0)
        calls = await s.ledger.get_calls(rid_of(r))
        assert [(c.outcome, c.charged_nano) for c in calls] == [(Outcome.CLIENT_ERROR, 0)]


@async_test
async def test_cooled_deployment_is_skipped_for_its_cooldown(tmp_path: Path) -> None:
    """A is skipped for 3 s (Retry-After beats the 1 s default); then one probe is let through."""
    async with open_stack(tmp_path, cooldown=1) as s:
        s.fake.enqueue(Script(status=429, retry_after="3"))
        await s.chat()
        s.clock.advance(seconds=2)
        await s.chat()
        assert hosts(s) == ["a", "b", "b"]  # still cooling at +2 s
        s.clock.advance(ms=1000)  # +3 s exactly: the cooldown has elapsed
        await s.chat()
        assert hosts(s) == ["a", "b", "b", "a"]


@async_test
async def test_both_throttled_is_upstream_throttled_and_released(tmp_path: Path) -> None:
    """Every attempt not_processed: 429 upstream_throttled (not budget_exhausted), hold released."""
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(status=429, retry_after="7"), Script(status=429, retry_after="2"))
        r = await s.chat()
        assert r.status_code == 429 and code_of(r) == "upstream_throttled"
        assert r.headers["retry-after"] == "7"
        assert r.json()["error"]["details"]["provider"] == "openai"
        assert hosts(s) == ["a", "b"]
        assert await s.totals() == (0, 0)
        row = await s.ledger.get_reservation(rid_of(r))
        assert row is not None and row.state is ReservationState.RELEASED
        calls = await s.ledger.get_calls(rid_of(r))
        assert [(c.attempt, c.outcome, c.charged_nano) for c in calls] == [
            (1, Outcome.THROTTLED, 0),
            (2, Outcome.THROTTLED, 0),
        ]
        assert "x-should-retry" not in r.headers  # retrying later is the right move


@async_test
async def test_attempts_never_exceed_max_attempts(tmp_path: Path) -> None:
    """Three deployments, max_attempts 2: the third is never tried."""
    async with open_stack(tmp_path, attempts=2) as s:
        s.fake.default = Script(status=429)
        r = await s.chat("alpha3")
        assert r.status_code == 429 and hosts(s) == ["a", "b"]
        assert len(await s.ledger.get_calls(rid_of(r))) == 2


@async_test
async def test_everything_cooling_releases_without_contact(tmp_path: Path) -> None:
    async with open_stack(tmp_path, attempts=2) as s:
        s.fake.default = Script(status=429, retry_after="30")
        first = await s.chat("alpha-one")
        assert first.status_code == 429 and hosts(s) == ["a"]
        second = await s.chat("alpha-one")  # A is cooling: no deployment to try at all
        assert second.status_code == 429 and code_of(second) == "upstream_throttled"
        assert int(second.headers["retry-after"]) >= 1
        assert hosts(s) == ["a"] and await s.totals() == (0, 0)
        row = await s.ledger.get_reservation(rid_of(second))
        assert row is not None and row.state is ReservationState.RELEASED


@async_test
async def test_all_unreachable_is_upstream_unavailable_and_free(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(refuse_connection=True), Script(refuse_connection=True))
        r = await s.chat()
        assert r.status_code == 502 and code_of(r) == "upstream_unavailable"
        assert s.fake.calls == 0 and await s.totals() == (0, 0)
        calls = await s.ledger.get_calls(rid_of(r))
        assert [c.outcome for c in calls] == [Outcome.UNREACHABLE, Outcome.UNREACHABLE]


@async_test
async def test_timeout_after_send_is_charged_in_full_without_failover(tmp_path: Path) -> None:
    sender = httpx.AsyncClient(
        transport=RaisingTransport(lambda r: httpx.ReadTimeout("slow", request=r))
    )
    async with open_stack(tmp_path, pipeline_kwargs={"sender": sender}) as s:
        r = await s.chat()
        assert r.status_code == 504 and code_of(r) == "upstream_unavailable"
        assert r.json()["error"]["type"] == "upstream_error"
        assert r.headers["x-should-retry"] == "false"
        held = s.observer.held[0].hold.nano
        assert await s.totals() == (held, 0)
        calls = await s.ledger.get_calls(rid_of(r))
        assert [c.outcome for c in calls] == [Outcome.TIMEOUT]  # one attempt: no failover
    await sender.aclose()


@async_test
async def test_connect_timeout_never_left_so_it_fails_over(tmp_path: Path) -> None:
    sender = httpx.AsyncClient(
        transport=RaisingTransport(lambda r: httpx.ConnectTimeout("slow", request=r))
    )
    async with open_stack(tmp_path, pipeline_kwargs={"sender": sender}) as s:
        r = await s.chat()
        assert r.status_code == 502 and "x-should-retry" not in r.headers
        calls = await s.ledger.get_calls(rid_of(r))
        assert [(c.outcome, c.charged_nano) for c in calls] == [(Outcome.UNREACHABLE, 0)] * 2
        assert await s.totals() == (0, 0)
    await sender.aclose()


@async_test
async def test_write_error_after_send_is_charged(tmp_path: Path) -> None:
    sender = httpx.AsyncClient(transport=RaisingTransport(lambda r: httpx.WriteError("x")))
    async with open_stack(tmp_path, pipeline_kwargs={"sender": sender}) as s:
        r = await s.chat()
        assert r.status_code == 502
        held = s.observer.held[0].hold.nano
        assert await s.totals() == (held, 0)
    await sender.aclose()
