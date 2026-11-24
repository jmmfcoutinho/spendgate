"""Observability: metric rendering, log redaction, health and /metrics endpoints."""

import asyncio
import io
import json
import logging

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from spendgate.core.money import Money
from spendgate.core.outcomes import Outcome
from spendgate.gateway.logs import JsonFormatter, configure_json_logging, log_call, log_event
from spendgate.gateway.metrics import Counter, Gauge, Histogram, Metrics
from spendgate.gateway.observability import (
    ReadinessChecks,
    compute_readiness,
    livez_endpoint,
    metrics_endpoint,
    readyz_endpoint,
)

SECRET = "sg-live-abcdef1234567890SECRET"  # noqa: S105 - fake fixture
PROMPT = "Tell me the confidential merger plan of Acme"


def test_render_hand_written_expected_output() -> None:
    c = Counter("t_total", 'Help with "quote" and \\ slash.', ("a",))
    c.inc(2, a='x"y\nz\\')
    g = Gauge("t_gauge", "G.")
    g.set(1.5)
    h = Histogram("t_ms", "H.", (10, 100), ("m",))
    h.observe(5, m="a")
    h.observe(50, m="a")
    h.observe(500, m="a")
    expected = (
        '# HELP t_total Help with "quote" and \\\\ slash.\n'
        "# TYPE t_total counter\n"
        't_total{a="x\\"y\\nz\\\\"} 2\n'
        "# HELP t_gauge G.\n# TYPE t_gauge gauge\nt_gauge 1.5\n"
        "# HELP t_ms H.\n# TYPE t_ms histogram\n"
        't_ms_bucket{m="a",le="10"} 1\n'
        't_ms_bucket{m="a",le="100"} 2\n'
        't_ms_bucket{m="a",le="+Inf"} 3\n'
        't_ms_sum{m="a"} 555\n'
        't_ms_count{m="a"} 3\n'
    )
    out = "\n".join(x for m in (c, g, h) for x in m.lines()) + "\n"
    assert out == expected


def test_metric_deltas_after_scripted_calls() -> None:
    m = Metrics()
    m.hold_opened()
    m.requests.inc(outcome="ok", model="gpt")
    m.upstream_latency.observe(120, model="gpt", deployment="d1")
    m.ttft.observe(40, model="gpt", deployment="d1")
    m.tokens.inc(10, kind="input")
    m.tokens.inc(5, kind="output")
    m.requests.inc(outcome="throttled", model="gpt")
    m.throttles.inc(model="gpt", deployment="d1")
    m.record_spend(nano=700, model="gpt", scope="org/team")
    m.set_remaining(scope="org/team", nano=300)
    m.record_cooldown(deployment="d1")
    m.record_overrun(scope="org/team")
    m.set_catalog(sha="abc", age_seconds=3600)
    m.set_catalog(sha="def", age_seconds=10)
    assert m.requests.value(outcome="ok", model="gpt") == 1
    assert m.throttles.value(model="gpt", deployment="d1") == 1
    assert m.tokens.value(kind="input") == 10
    assert m.upstream_latency.count(model="gpt", deployment="d1") == 1
    assert m.holds_in_flight.value() == 1
    m.hold_closed()
    out = m.render()
    assert "spendgate_holds_in_flight 0\n" in out
    assert 'spendgate_spend_nano_total{model="gpt",scope="org/team"} 700\n' in out
    assert 'spendgate_budget_remaining_nano{scope="org/team"} 300\n' in out
    assert 'spendgate_catalog_info{sha="def"} 1\n' in out
    assert 'sha="abc"' not in out
    assert "spendgate_catalog_age_seconds 10\n" in out
    assert 'spendgate_cooldowns_total{deployment="d1"} 1\n' in out


def test_cr_07_an_unknown_catalog_age_is_not_published_as_fresh() -> None:
    """CR-07: no age known means no age gauge, not a gauge reading 0."""
    metrics = Metrics()
    metrics.set_catalog(sha="abc", age_seconds=None)
    assert "\nspendgate_catalog_age_seconds " not in metrics.render()
    metrics.set_catalog(sha="abc", age_seconds=5)
    assert "\nspendgate_catalog_age_seconds 5\n" in metrics.render()
    metrics.set_catalog(sha="abc", age_seconds=None)  # and it goes away again
    assert "\nspendgate_catalog_age_seconds " not in metrics.render()


def test_wrong_label_set_is_rejected() -> None:
    m = Metrics()
    try:
        m.requests.inc(outcome="ok", model="m", user="alice")
    except ValueError:
        return
    raise AssertionError("extra label accepted")


def _capture() -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    return configure_json_logging(logging.StreamHandler(buf)), buf


# T-LOG-01
def test_logs_contain_no_prompt_or_secret() -> None:
    logger, buf = _capture()
    log_call(
        logger, request_id="r1", key_prefix_=SECRET, scope_path="org/team", model="gpt",
        deployment="d1", outcome=Outcome.OK, input_tokens=3, output_tokens=4,
        cost=Money.from_amount("0.5", "EUR"), latency_ms=12.5,
    )  # fmt: skip
    log_event(
        logger, "call", request_id="r2", prompt=PROMPT, authorization=f"Bearer {SECRET}",
        body={"messages": [PROMPT]}, error=f"failed with {SECRET}", model=SECRET,
        extra_headers={"x": SECRET},
    )  # fmt: skip
    logger.error("boom %s", PROMPT, exc_info=(ValueError, ValueError(SECRET), None))
    out = buf.getvalue()
    assert PROMPT not in out
    assert SECRET not in out
    assert "confidential" not in out
    first = out.splitlines()[0]
    assert '"key_prefix":"sg-live-"' in first
    assert '"cost":"0.500000000"' in first
    assert '"currency":"EUR"' in first


def test_logs_drop_unknown_fields_and_are_json_lines() -> None:
    logger, buf = _capture()
    log_event(logger, "call", request_id="r", prompt=PROMPT, body="secret body", tag="t")
    rec = json.loads(buf.getvalue())
    assert set(rec) == {"event", "request_id", "ts", "level"}
    assert isinstance(JsonFormatter(), logging.Formatter)


def _checks(
    *, ledger: bool = True, catalog: bool = True, kill: bool = False, db: int = 1_000_000,
    now: int = 1_000_000,
) -> ReadinessChecks:  # fmt: skip
    async def ledger_ok() -> bool:
        if not ledger:
            raise ConnectionError("postgres://user:pw@host/db")
        return True

    async def db_now() -> int:
        return db

    return ReadinessChecks(ledger_ok, lambda: catalog, lambda: kill, db_now, lambda: now)


def test_readiness_ok_and_failures() -> None:
    ok = asyncio.run(compute_readiness(_checks()))
    assert ok.ready and not ok.warnings
    bad = asyncio.run(compute_readiness(_checks(ledger=False, catalog=False, kill=True)))
    assert not bad.ready
    assert bad.reasons == ("ledger_unreachable", "catalog_not_loaded", "kill_switch_engaged")
    assert "postgres" not in str(bad.body())


# T-CLK-03
def test_clock_skew_warns_without_failing() -> None:
    skewed = asyncio.run(compute_readiness(_checks(db=1_005_000)))
    assert skewed.ready and len(skewed.warnings) == 1 and "5000 ms" in skewed.warnings[0]
    edge = asyncio.run(compute_readiness(_checks(db=1_002_000)))
    assert edge.ready and not edge.warnings


def test_endpoints() -> None:
    m = Metrics()
    m.requests.inc(outcome="ok", model="gpt")
    state = {"ledger": True}

    async def ledger_ok() -> bool:
        return state["ledger"]

    async def db_now() -> int:
        return 0

    checks = ReadinessChecks(ledger_ok, lambda: True, lambda: False, db_now, lambda: 0)

    async def deny(request: Request) -> Response | None:
        if request.headers.get("x-admin") == "1":
            return None
        return PlainTextResponse("no", status_code=401)

    app = Starlette(
        routes=[
            Route("/livez", livez_endpoint()),
            Route("/readyz", readyz_endpoint(checks)),
            Route("/metrics", metrics_endpoint(m)),
            Route("/guarded", metrics_endpoint(m, deny)),
        ]
    )
    client = TestClient(app)
    assert client.get("/livez").status_code == 200
    assert client.get("/readyz").status_code == 200
    state["ledger"] = False
    r = client.get("/readyz")
    assert (r.status_code, r.json()) == (503, {"status": "not_ready"})  # no detail here (SR-06)
    assert client.get("/livez").status_code == 200
    r = client.get("/metrics")
    assert r.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert 'spendgate_requests_total{outcome="ok",model="gpt"} 1' in r.text
    assert client.get("/guarded").status_code == 401
    assert client.get("/guarded", headers={"x-admin": "1"}).status_code == 200


def test_request_ids_stay_in_the_logs() -> None:
    logger, buf = _capture()
    rid = "sg_req_01jabcdefghjkmnpqrstvwxyz0"  # pragma: allowlist secret
    log_event(logger, "call", request_id=rid)
    assert rid in buf.getvalue()
