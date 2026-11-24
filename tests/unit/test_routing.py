"""The router (cooldown bookkeeping, candidate order) and the sent/never-sent table."""

import random
from pathlib import Path

import httpx

from spendgate.config import Settings
from spendgate.pipeline.candidates import Cause, Router
from spendgate.pipeline.transport import classify
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
    assert classify(httpx.ConnectError("secret-host.test")).kind == "ConnectError"  # no message
