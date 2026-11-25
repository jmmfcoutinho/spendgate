"""OpenTelemetry tracing (issue #34): one span per hold, forward and settle, allowlisted attributes.

An in-memory exporter records the spans. The caller's `traceparent` is the parent of every span
and reaches the provider with the forward span's id; nothing the caller made up (prompt, key,
user, session, tags, `tracestate`) reaches a span attribute or the provider. Without a tracer the
pipeline makes no span and adds no header.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("opentelemetry.sdk", reason="needs the otel extra")

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind

from spendgate.cli import serve
from spendgate.core.policy import SESSION_HEADER, TAGS_HEADER, USER_HEADER
from spendgate.core.sessions import SessionDefaults
from spendgate.gateway import otel
from spendgate.pipeline import tracing
from tests.fakes import FakeUpstream
from tests.fakes.script import Script
from tests.integration.harness import Stack, async_test, open_stack
from tests.integration.test_gateway_streaming import gated, open_call
from tests.integration.test_otel_off import otel_settings

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_ID}-01"
PROMPT = "prompt-fake-4711 please summarise"
CALLER_MADE = {
    SESSION_HEADER: "sess-fake-4711", USER_HEADER: "user-fake-4711",
    TAGS_HEADER: "team-fake-4711", "tracestate": "vendor=fake-4711",
}  # fmt: skip
NAMES = [tracing.HOLD, tracing.FORWARD, tracing.SETTLE]


@asynccontextmanager
async def traced(
    tmp_path: Path, fake: FakeUpstream | None = None, **kw: object
) -> AsyncIterator[tuple[Stack, InMemorySpanExporter, FakeUpstream]]:
    exporter = InMemorySpanExporter()
    started = otel.start("spendgate-test", SimpleSpanProcessor(exporter))
    fake = fake or FakeUpstream()
    client = fake.client()
    extra: dict[str, object] = {"tracer": started.tracer, "sender": otel.TracingSender(client)}
    try:
        async with open_stack(tmp_path, fake=fake, pipeline_kwargs=extra, **kw) as s:  # type: ignore[arg-type]
            yield s, exporter, fake
    finally:
        await client.aclose()
        started.shutdown()


def chat_body() -> dict[str, object]:
    return {"model": "alpha", "max_tokens": 100, "messages": [{"role": "user", "content": PROMPT}]}


def assert_allowlisted(spans: tuple[ReadableSpan, ...], secret: str) -> None:
    """T-OBS-04: allowlisted names only, nothing the caller made up, no exception text."""
    for span in spans:
        attributes = dict(span.attributes or {})
        assert set(attributes) <= otel.ALLOWED_ATTRIBUTES, span.name
        text = repr(attributes)
        assert "4711" not in text and secret not in text and secret[:12] not in text
        assert not span.events  # exceptions are never recorded: their messages may quote data


@async_test
async def test_a_call_makes_three_spans_under_the_callers_traceparent(tmp_path: Path) -> None:
    sessions = SessionDefaults(max_steps=10)
    async with traced(tmp_path, session_defaults=sessions) as (s, exporter, fake):
        headers = {"traceparent": TRACEPARENT, **CALLER_MADE}
        reply = await s.post("/v1/chat/completions", chat_body(), headers=headers)
        assert reply.status_code == 200, reply.text
        await s.pipeline.drain()
        spans = exporter.get_finished_spans()
        assert [span.name for span in spans] == NAMES
        for span in spans:
            assert span.context is not None and span.parent is not None
            assert span.context.trace_id == int(TRACE_ID, 16)
            assert span.parent.span_id == int(PARENT_ID, 16)
        hold, forward, settle = spans
        assert forward.kind is SpanKind.CLIENT and hold.kind is SpanKind.INTERNAL
        assert dict(hold.attributes or {}) == {
            "spendgate.request_id": reply.headers["x-spendgate-request-id"],
            "spendgate.model": "openai/gpt-6-luna",
            "spendgate.endpoint": "/v1/chat/completions",
            "spendgate.outcome": "held",
        }
        assert (forward.attributes or {})["spendgate.attempts"] == 1
        assert (settle.attributes or {})["spendgate.outcome"] == "ok"
        assert (settle.attributes or {})["spendgate.deployment"] == "alpha/0"
        assert (settle.attributes or {})["spendgate.booked"] is True
        assert_allowlisted(spans, s.secret)
        sent = fake.requests[0].headers
        assert forward.context is not None
        assert sent["traceparent"] == f"00-{TRACE_ID}-{forward.context.span_id:016x}-01"
        assert "tracestate" not in sent and "baggage" not in sent


@async_test
async def test_without_a_traceparent_the_hold_starts_the_trace_for_its_call(tmp_path: Path) -> None:
    async with traced(tmp_path) as (s, exporter, fake):
        for traceparent in (None, "00-not-a-trace-01"):
            headers = {"traceparent": traceparent} if traceparent else {}
            assert (await s.post("/v1/chat/completions", chat_body(), headers=headers)).is_success
        await s.pipeline.drain()
        spans = exporter.get_finished_spans()
        assert [span.name for span in spans] == NAMES * 2
        for hold, forward, settle in (spans[:3], spans[3:]):
            assert hold.parent is None and hold.context is not None
            for child in (forward, settle):
                assert child.parent is not None and child.parent.span_id == hold.context.span_id
                assert child.context is not None
                assert child.context.trace_id == hold.context.trace_id
        assert spans[0].context is not None and spans[3].context is not None
        assert spans[0].context.trace_id != spans[3].context.trace_id  # one trace per call
        assert all(r.headers["traceparent"].startswith("00-") for r in fake.requests)


@async_test
async def test_a_refused_hold_is_one_span_and_nothing_is_sent(tmp_path: Path) -> None:
    async with traced(tmp_path, budget="0.000001") as (s, exporter, _fake):
        headers = {"traceparent": TRACEPARENT}
        reply = await s.post("/v1/chat/completions", chat_body(), headers=headers)
        assert reply.json()["error"]["code"] == "budget_exhausted"
        s.assert_no_contact()
        spans = exporter.get_finished_spans()
        assert [span.name for span in spans] == [tracing.HOLD]
        assert (spans[0].attributes or {})["spendgate.outcome"] == "budget_exhausted"


@async_test
async def test_a_call_the_provider_never_processed_is_a_released_settle_span(
    tmp_path: Path,
) -> None:
    async with traced(tmp_path, fake=FakeUpstream(Script(status=429))) as (s, exporter, fake):
        assert (await s.chat()).status_code == 429
        await s.pipeline.drain()
        spans = exporter.get_finished_spans()
        assert [span.name for span in spans] == NAMES
        assert (spans[1].attributes or {})["spendgate.attempts"] == fake.calls == 2
        assert (spans[2].attributes or {})["spendgate.released"] is True
        assert_allowlisted(spans, s.secret)


@async_test
async def test_a_stream_settles_its_span_when_it_ends(tmp_path: Path) -> None:
    exporter = InMemorySpanExporter()
    started = otel.start("spendgate-test", SimpleSpanProcessor(exporter))
    try:
        async with gated(tmp_path, pipeline_kwargs={"tracer": started.tracer}) as (s, up):
            call = await open_call(s)
            assert [span.name for span in exporter.get_finished_spans()] == NAMES[:2]
            await call.disconnect()  # the client leaves: the stream settles itself
            await call.finish()
            await s.pipeline.aclose()
            spans = exporter.get_finished_spans()
            assert [span.name for span in spans] == NAMES
            assert (spans[2].attributes or {})["spendgate.outcome"] == "cancelled"
            assert len({span.context.trace_id for span in spans if span.context}) == 1
            assert up.streams
    finally:
        started.shutdown()


def test_attributes_off_the_allowlist_or_shaped_like_a_key_are_dropped() -> None:
    kept = otel.allowed({
        "spendgate.model": "openai/gpt-6-luna", "spendgate.outcome": "sk-fake0123456789abcdef",
        "spendgate.deployment": "x" * 300, "user": "user-fake", "spendgate.attempts": 2,
        "spendgate.booked": True, "spendgate.endpoint": ["/v1/chat/completions"],
    })  # fmt: skip
    assert kept == {"spendgate.model": "openai/gpt-6-luna", "spendgate.attempts": 2,
                    "spendgate.booked": True}  # fmt: skip


def test_a_failing_step_sets_only_the_error_type_on_its_span() -> None:
    exporter = InMemorySpanExporter()
    started = otel.start("spendgate-test", SimpleSpanProcessor(exporter))
    tracer = started.tracer
    parent = tracer.parent({"traceparent": TRACEPARENT})
    span_cm = tracer.span(tracing.HOLD, parent, {"spendgate.model": "m"})
    with pytest.raises(ValueError, match="sk-fake"), span_cm as span:
        span.set("spendgate.prompt", PROMPT)  # off the allowlist: dropped
        raise ValueError("sk-fake0123456789abcdef in a message")
    (recorded,) = exporter.get_finished_spans()
    assert dict(recorded.attributes or {}) == {"spendgate.model": "m", "error.type": "ValueError"}
    assert not recorded.events and recorded.status.description is None
    started.shutdown()


def test_a_span_that_fails_to_start_leaves_the_steps_error_unchained() -> None:
    class Broken:
        def start_span(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("tracer bug")

    tracer = otel.OtelTracer(Broken())  # type: ignore[arg-type]
    span = tracer.span(tracing.HOLD, tracer.parent({}), {"spendgate.model": "m"})
    with pytest.raises(ValueError, match="the step failed") as raised, span:
        raise ValueError("the step failed")
    assert raised.value.__context__ is None  # not the tracer's RuntimeError


# --- on when enabled and installed (the off paths are in test_otel_off.py) ----------------------


@async_test
async def test_tracing_is_on_only_when_enabled_and_installed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async with open_stack(tmp_path) as s:
        async with serve.upstream(otel_settings(s, enabled=False)) as (sender, tracer):
            assert tracer is None and not isinstance(sender, otel.TracingSender)
        async with serve.upstream(otel_settings(s, enabled=True)) as (sender, tracer):
            assert isinstance(tracer, otel.OtelTracer)
            assert isinstance(sender, otel.TracingSender)
    assert serve.OTEL_MISSING not in capsys.readouterr().err
