"""The router (cooldown bookkeeping, candidate order) and the sent/never-sent table."""

import random
from pathlib import Path

import httpx

from spendgate.config import Settings
from spendgate.core.errors import ErrorCode
from spendgate.core.failures import AttemptError
from spendgate.core.health import HealthConfig
from spendgate.pipeline.candidates import Cause, Router, retry_hint
from spendgate.pipeline.routing import MAX_WAIT_MS
from spendgate.pipeline.transport import classify
from spendgate.providers.port import UpstreamError
from tests.conftest import FakeClock
from tests.unit.test_pipeline_steps import settings as make_settings

NOW = 1_000_000


def router(tmp_path: Path) -> tuple[Router, Settings]:
    s = make_settings(tmp_path)
    return Router(s.routes, s.health), s


def test_candidates_keep_config_order_and_labels(tmp_path: Path) -> None:
    r, _ = router(tmp_path)
    assert [t.label for t in r.targets("alpha")] == ["alpha/0", "alpha/1"]
    assert r.targets("alpha")[0].deployment.credential == "sk-test-openai-not-real"


def test_pick_skips_tried_and_cooling(tmp_path: Path) -> None:
    r, _ = router(tmp_path)
    a, b = r.targets("alpha")
    assert r.pick((a, b), set(), NOW) is a
    assert r.pick((a, b), {"alpha/0"}, NOW) is b
    assert r.pick((a, b), {"alpha/0", "alpha/1"}, NOW) is None
    r.cool(a, NOW, None, Cause.THROTTLED)
    assert r.pick((a, b), set(), NOW) is b


def test_retry_after_extends_the_cooldown_and_is_capped(tmp_path: Path) -> None:
    r, s = router(tmp_path)
    a = r.targets("alpha")[0]
    assert r.cool(a, NOW, 3, Cause.THROTTLED) == 3_000  # honoured as given, under the 5 s default
    assert r.cool(a, NOW, 60, Cause.THROTTLED) == 60_000
    assert r.cool(a, NOW, 10**6, Cause.THROTTLED) == s.health.max_cooldown_ms  # a hostile header
    assert r.wait_seconds([a], NOW) == 300
    assert r.all_throttled([a]) and not r.all_throttled(r.targets("alpha"))


def test_cooldown_ends_on_time_with_the_fake_clock(tmp_path: Path) -> None:
    r, _ = router(tmp_path)
    clock = FakeClock(NOW)
    a, b = r.targets("alpha")
    r.cool(a, clock.now(), 3, Cause.THROTTLED)
    clock.advance(seconds=2)
    assert r.pick((a, b), set(), clock.now()) is b
    clock.advance(seconds=1)
    assert r.pick((a, b), set(), clock.now()) is a
    assert r.pick((a, b), set(), clock.now()) is a  # back for everyone: a 429 is no breaker trip


def test_a_429_without_retry_after_cools_for_a_seeded_jittered_moment(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    first = Router(s.routes, s.health, random.Random(5).randint)  # noqa: S311 - jitter
    again = Router(s.routes, s.health, random.Random(5).randint)  # noqa: S311 - jitter
    cooled = [first.cool(t, NOW, None, Cause.THROTTLED) for t in first.targets("alpha")]
    assert cooled == [again.cool(t, NOW, None, Cause.THROTTLED) for t in again.targets("alpha")]
    assert all(s.health.throttle_min_ms <= ms <= s.health.throttle_max_ms for ms in cooled)


def test_a_quota_429_cools_for_the_full_breaker_cooldown(tmp_path: Path) -> None:
    r, s = router(tmp_path)
    a = r.targets("alpha")[0]
    assert r.cool(a, NOW, None, Cause.QUOTA) == s.health.cooldown_ms  # billing will not clear
    assert r.cool(a, NOW, 60, Cause.QUOTA) == 60_000


def test_a_late_result_keeps_the_cooling_cause(tmp_path: Path) -> None:
    """A call sent before the 429 that answers after it must not relabel the cooldown."""
    r, _ = router(tmp_path)
    a, b = r.targets("alpha")
    r.cool(a, NOW, 30, Cause.THROTTLED)
    r.cool(b, NOW, 30, Cause.THROTTLED)
    r.succeeded(a, NOW + 10)
    r.failed(b, NOW + 10)
    assert a.shared.cause is b.shared.cause is Cause.THROTTLED
    assert r.all_throttled((a, b))  # so nothing-to-try stays upstream_throttled, not a 503


def test_soonest_waits_only_for_untried_busy_deployments(tmp_path: Path) -> None:
    r, s = router(tmp_path)
    a, b = r.targets("alpha")
    assert r.soonest((a, b), set(), NOW) is None  # nothing is cooling
    r.cool(a, NOW, 2, Cause.THROTTLED)
    r.cool(b, NOW, 1, Cause.THROTTLED)
    assert r.soonest((a, b), set(), NOW) == 1_000
    assert r.soonest((a, b), {"alpha/1"}, NOW) == 2_000  # a tried deployment is not waited for
    r.cool(b, NOW, None, Cause.QUOTA)
    assert r.soonest((b,), set(), NOW) is None  # waiting does not fix a quota fault
    for _ in range(s.health.min_requests):
        r.failed(a, NOW + 3_000)  # a's cooldown is over: these trip its breaker
    assert r.pick((a,), set(), NOW + 3_000 + s.health.cooldown_ms) is a  # the one probe
    assert r.soonest((a,), set(), NOW + 3_000 + s.health.cooldown_ms) is None  # never spin


def test_success_clears_the_cause_and_failures_trip_the_breaker(tmp_path: Path) -> None:
    r, s = router(tmp_path)
    a = r.targets("alpha")[0]
    r.cool(a, NOW, None, Cause.UNREACHABLE)
    r.succeeded(a, NOW)
    assert a.shared.cause is Cause.UNREACHABLE  # still cooling: the result was ignored
    r.succeeded(a, NOW + s.health.throttle_max_ms)
    assert a.shared.cause is None
    for _ in range(s.health.min_requests):
        r.failed(a, NOW)
    assert a.shared.cause is Cause.FAILING and r.pick([a], set(), NOW) is None


def test_aliases_share_the_health_of_one_real_endpoint(tmp_path: Path) -> None:
    r, _ = router(tmp_path)
    assert r.targets("alpha-b")[0].shared is r.targets("alpha-twin")[0].shared  # same endpoint
    assert r.targets("alpha")[0].shared is not r.targets("alpha-b")[0].shared  # different URL


def test_never_sent_versus_sent_table() -> None:
    req = httpx.Request("POST", "https://x.test")
    never = [httpx.ConnectError("x"), httpx.ConnectTimeout("x"), httpx.PoolTimeout("x"),
             httpx.ProxyError("x"), httpx.UnsupportedProtocol("x")]  # fmt: skip
    sent = [httpx.WriteError("x"), httpx.WriteTimeout("x"), httpx.ReadError("x"),
            httpx.ReadTimeout("x"), httpx.RemoteProtocolError("x"), httpx.LocalProtocolError("x"),
            httpx.DecodingError("x"), httpx.TooManyRedirects("x", request=req), OSError("x"),
            RuntimeError("x")]  # fmt: skip
    assert all(not classify(e).sent for e in never)
    assert all(classify(e).sent for e in sent)
    assert classify(httpx.ReadTimeout("x")).timeout and not classify(httpx.ReadError("x")).timeout
    assert (
        classify(httpx.ConnectTimeout("x")).timeout and not classify(httpx.ConnectTimeout("x")).sent
    )
    assert classify(httpx.ConnectError("secret-host.test")).code is AttemptError.CONNECT_ERROR


def test_the_wait_bound_outlasts_a_no_hint_cooldown() -> None:
    assert HealthConfig().throttle_max_ms <= MAX_WAIT_MS


def test_soonest_never_waits_for_a_failing_or_unreachable_deployment(tmp_path: Path) -> None:
    """Waiting for a breaker would hand its half-open probe to the waiting call."""
    r, s = router(tmp_path)
    a, b = r.targets("alpha")
    for _ in range(s.health.min_requests):
        r.failed(a, NOW)
    assert a.shared.cause is Cause.FAILING and r.soonest((a,), set(), NOW) is None
    r.cool(b, NOW, None, Cause.UNREACHABLE)
    assert r.soonest((b,), set(), NOW) is None
    r.cool(a, NOW, None, Cause.THROTTLED)  # a 429 on a failure-opened breaker: still a breaker
    assert not a.shared.health.busy and r.soonest((a,), set(), NOW) is None


def test_each_waiter_draws_its_own_jitter_from_the_router_rng(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    first = Router(s.routes, s.health, random.Random(9).randint)  # noqa: S311 - jitter
    again = Router(s.routes, s.health, random.Random(9).randint)  # noqa: S311 - jitter
    drawn = [first.jitter() for _ in range(20)]
    assert drawn == [again.jitter() for _ in range(20)]
    assert all(0 <= ms <= s.health.throttle_min_ms for ms in drawn) and len(set(drawn)) > 1


def _throttled(seconds: int | None = None) -> UpstreamError:
    return UpstreamError(ErrorCode.UPSTREAM_THROTTLED, True, 429, seconds)


def test_retry_hint_reads_an_http_date_against_the_clock() -> None:
    at = {"Retry-After": "Tue, 17 Nov 2026 00:00:03 GMT"}
    epoch = 1_794_873_600_000  # 2026-11-17T00:00:00Z
    assert retry_hint(_throttled(), at, epoch) == (3, False)
    assert retry_hint(_throttled(), at, epoch + 2_500) == (1, False)  # rounded up
    assert retry_hint(_throttled(), at, epoch + 60_000) == (0, False)  # past: the short draw
    assert retry_hint(_throttled(), {"retry-after": "Sat, 01 Jan 2028 00:00:00 GMT"}, epoch) == (
        86_400,
        False,
    )  # capped at a day, like delta-seconds
    assert retry_hint(_throttled(7), at, epoch) == (7, False)  # the adapter's seconds win


def test_retry_hint_marks_a_garbage_header_unreadable() -> None:
    assert retry_hint(_throttled(), {"Retry-After": "soon-ish"}, NOW) == (None, True)
    assert retry_hint(_throttled(), {"retry-after-ms": "-5"}, NOW) == (None, True)
    assert retry_hint(_throttled(), {}, NOW) == (None, False)  # no header: the short draw


def test_an_unreadable_retry_after_cools_for_the_breaker_cooldown(tmp_path: Path) -> None:
    r, s = router(tmp_path)
    a = r.targets("alpha")[0]
    assert r.cool(a, NOW, None, Cause.THROTTLED, unknown=True) == s.health.cooldown_ms


def test_soonest_does_not_wait_for_an_escalated_cooldown(tmp_path: Path) -> None:
    """Throttled again on its return: waiters would only meet it again; new arrivals try it."""
    r, s = router(tmp_path)
    a = r.targets("alpha")[0]
    r.cool(a, NOW, None, Cause.THROTTLED)
    assert r.soonest((a,), set(), NOW) is not None  # a first busy cooldown is waited for
    back = NOW + s.health.throttle_max_ms
    assert r.pick((a,), set(), back) is a
    r.cool(a, back, None, Cause.THROTTLED)
    assert a.shared.health.escalated and r.soonest((a,), set(), back) is None


def test_succeeded_passes_the_start_so_an_old_call_keeps_the_escalation(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    r = Router(s.routes, s.health, lambda low, high: high)  # the longest draw: exact numbers
    a = r.targets("alpha")[0]
    r.cool(a, NOW, None, Cause.THROTTLED)
    back = NOW + s.health.throttle_max_ms
    r.cool(a, back, None, Cause.THROTTLED)
    after = back + 2 * s.health.throttle_max_ms
    r.succeeded(a, after, started=back - 1)  # sent before the second trip
    assert r.cool(a, after, None, Cause.THROTTLED) == 4 * s.health.throttle_max_ms


def test_retry_hint_falls_back_to_a_valid_retry_after_ms() -> None:
    assert retry_hint(_throttled(), {"retry-after-ms": "200"}, NOW) == (1, False)  # rounded up
