"""The router (cooldown bookkeeping, candidate order) and the sent/never-sent table."""

from pathlib import Path

import httpx

from spendgate.config import Settings
from spendgate.pipeline.routing import Cause, Router
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
    assert r.cool(a, NOW, 3, Cause.THROTTLED) == 5_000  # the 5 s default beats a 3 s hint
    assert r.cool(a, NOW, 60, Cause.THROTTLED) == 60_000
    assert r.cool(a, NOW, 10**6, Cause.THROTTLED) == s.health.max_cooldown_ms  # a hostile header
    assert r.wait_seconds([a], NOW) == 300
    assert r.all_throttled([a]) and not r.all_throttled(r.targets("alpha"))


def test_cooldown_ends_on_time_with_the_fake_clock(tmp_path: Path) -> None:
    r, _ = router(tmp_path)
    clock = FakeClock(NOW)
    a, b = r.targets("alpha")
    r.cool(a, clock.now(), 3, Cause.THROTTLED)  # 5 s default cooldown wins
    clock.advance(seconds=4)
    assert r.pick((a, b), set(), clock.now()) is b
    clock.advance(seconds=1)
    assert r.pick((a, b), set(), clock.now()) is a  # one probe


def test_success_clears_the_cause_and_failures_trip_the_breaker(tmp_path: Path) -> None:
    r, s = router(tmp_path)
    a = r.targets("alpha")[0]
    r.cool(a, NOW, None, Cause.UNREACHABLE)
    r.succeeded(a, NOW)
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
