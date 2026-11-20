"""SSE streaming with exact settlement on every ending (M-13, T-MON-11, C-22).

The rig reads the gateway chunk by chunk and lets the test release the upstream's frames one at a
time, so "no buffering", "cut" and "client leaves" are observable. Every test ends with the same
check: the hold is gone, the money moved exactly once, and the observer saw the hold close.
"""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from spendgate.core.cost import Usage, actual_cost
from spendgate.core.outcomes import Outcome, ReservationState
from spendgate.pipeline.model import CHAT as CHAT_ENDPOINT
from spendgate.providers.port import SseLimits
from tests.fakes import ABSURD_USAGE, FakeUsage, Script
from tests.integration.harness import CHAT, NOW_MS, Stack, async_test, open_stack
from tests.integration.stream_rig import (
    DONE,
    AsgiCall,
    GatedUpstream,
    chat_chunk,
    chat_usage,
)

BODY: dict[str, object] = {
    "model": "alpha", "max_tokens": 100, "stream": True,
    "messages": [{"role": "user", "content": "hi"}],
}  # fmt: skip
SEEN = FakeUsage(input_tokens=10, output_tokens=5)


@asynccontextmanager
async def gated(tmp_path: Path, **kw: object) -> AsyncIterator[tuple[Stack, GatedUpstream]]:
    upstream = GatedUpstream()
    client = httpx.AsyncClient(transport=upstream)
    extra = kw.pop("pipeline_kwargs", {}) or {}
    try:
        async with open_stack(
            tmp_path,
            pipeline_kwargs={"sender": client, **extra},  # type: ignore[dict-item]
            **kw,  # type: ignore[arg-type]
        ) as s:
            yield s, upstream
    finally:
        await client.aclose()


async def open_call(s: Stack, body: dict[str, object] | None = None) -> AsgiCall:
    return await AsgiCall(s.app, CHAT, body or BODY, s.secret).start()


def priced(s: Stack, usage: FakeUsage = SEEN) -> int:
    entry = s.settings.catalog.lookup("openai", "gpt-6-luna", NOW_MS)
    cost = actual_cost(
        entry, Usage(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
    )
    return s.settings.catalog.in_ledger(cost).nano


async def settled_once(s: Stack, outcome: Outcome, charged: int) -> None:
    """The invariant after any ending: no hold left, one transition, one streamed calls row."""
    await s.pipeline.aclose()
    assert await s.totals() == (charged, 0)
    held = s.observer.held[0]
    rows = await s.ledger.get_calls(held.request_id)
    assert [(c.outcome, c.charged_nano) for c in rows] == [(outcome, charged)]
    res = await s.ledger.get_reservation(held.request_id)
    assert res is not None and res.state is ReservationState.SETTLED
    assert len(s.observer.held) == len(s.observer.settled) == 1  # holds in flight balance
    assert s.observer.settled[0].outcome is outcome


# --- the endings ------------------------------------------------------------------------------


@async_test
async def test_stream_is_relayed_as_it_comes_and_settled_from_usage(tmp_path: Path) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        assert call.status == 200
        assert call.headers["content-type"] == "text/event-stream"
        assert call.headers["cache-control"] == "no-store"
        assert call.headers["x-spendgate-request-id"].startswith("sg_req_")
        assert "x-spendgate-cost" not in call.headers  # not known before the stream ends
        assert call.headers["x-spendgate-budget-scope"] == "org:acme/user:u1"
        assert (await s.totals())[1] > 0  # the hold is open while the stream runs
        up.streams[0].put(chat_chunk("Hel"))
        assert await call.chunk() == chat_chunk("Hel")  # delivered before the upstream finished
        up.streams[0].put(chat_chunk("lo"))
        up.streams[0].put(chat_usage(SEEN))
        up.streams[0].put(DONE)
        up.streams[0].end()
        text = (await call.read_all()).decode()
        assert text.count("data: ") == 2 + 1  # two deltas, [DONE]; the usage chunk is stripped
        cost = f": spendgate request_id={s.observer.held[0].request_id} cost="
        assert cost in text and text.index(": spendgate") < text.index("data: [DONE]")
        await call.finish()
        assert up.streams[0].closed
        await settled_once(s, Outcome.OK, priced(s))
        event = s.observer.settled[0]
        assert event.ttft_ms is not None and event.remaining is not None
        assert event.scope_path == "org:acme/user:u1"
        (row,) = await s.ledger.get_calls(event.request_id)
        assert row.usage_cost_nano == priced(s)
        sent = up.requests[0].read()
        assert b'"include_usage":true' in sent.replace(b" ", b"")  # forced upstream


@async_test
async def test_end_without_usage_is_charged_the_full_hold(tmp_path: Path) -> None:  # S-02
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        up.streams[0].put(chat_chunk("Hi"))
        up.streams[0].put(DONE)
        up.streams[0].end()
        await call.read_all()
        await call.finish()
        await settled_once(s, Outcome.OK_NO_USAGE, s.observer.held[0].hold.nano)


@async_test
async def test_cut_mid_stream_is_stream_broken_at_the_full_hold(tmp_path: Path) -> None:  # M-13
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        up.streams[0].put(chat_chunk("Hi"))
        up.streams[0].cut()
        text = (await call.read_all()).decode()
        assert '"error"' in text and text.endswith("data: [DONE]\n\n")  # protocol 2.8, chat
        assert ": spendgate" not in text  # a broken stream carries no cost comment
        await call.finish()
        await settled_once(s, Outcome.STREAM_BROKEN, s.observer.held[0].hold.nano)
        assert up.streams[0].closed


@async_test
async def test_cut_after_usage_is_charged_the_usage(tmp_path: Path) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        up.streams[0].put(chat_chunk("Hi") + chat_usage(SEEN))
        up.streams[0].cut()
        await call.read_all()
        await call.finish()
        await settled_once(s, Outcome.STREAM_BROKEN, priced(s))


@async_test
async def test_client_disconnect_aborts_upstream_and_is_charged(tmp_path: Path) -> None:  # T-MON-11
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        up.streams[0].put(chat_chunk("Hi"))
        assert await call.chunk() is not None
        await call.disconnect()
        assert up.streams[0].closed  # the upstream call was torn down
        await settled_once(s, Outcome.CANCELLED, s.observer.held[0].hold.nano)


@async_test
async def test_client_disconnect_after_usage_is_charged_the_usage(tmp_path: Path) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        up.streams[0].put(chat_chunk("Hi") + chat_usage(SEEN))
        assert await call.chunk() is not None
        await call.disconnect()
        await settled_once(s, Outcome.CANCELLED, priced(s))


@async_test
async def test_client_gone_while_the_upstream_is_silent(tmp_path: Path) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)  # no frame was ever released
        await call.disconnect()
        assert up.streams[0].closed
        await settled_once(s, Outcome.CANCELLED, s.observer.held[0].hold.nano)


@async_test
async def test_a_stream_nobody_iterates_is_settled_by_aclose(tmp_path: Path) -> None:
    """A reply whose adapter vanished before the first byte must not leave a hold behind."""
    async with gated(tmp_path) as (s, up):
        sent = {"Authorization": f"Bearer {s.secret}", "Content-Type": "application/json"}
        reply = await s.pipeline.serve(CHAT_ENDPOINT, sent, _one(json.dumps(BODY).encode()))
        assert reply.stream is not None and (await s.totals())[1] > 0
        await s.pipeline.aclose()  # the app is going down: abandoned streams settle
        assert up.streams[0].closed
        await settled_once(s, Outcome.CANCELLED, s.observer.held[0].hold.nano)


async def _one(raw: bytes) -> AsyncIterator[bytes]:
    yield raw


@async_test
async def test_idle_upstream_gets_keepalive_comments(tmp_path: Path) -> None:
    async with gated(tmp_path, pipeline_kwargs={"ping_seconds": 0.01}) as (s, up):
        call = await open_call(s)
        assert await call.chunk() == b": ping\n\n"  # nothing upstream yet; the client is kept warm
        up.streams[0].put(chat_chunk("Hi"))
        up.streams[0].put(chat_usage(SEEN))
        up.streams[0].put(DONE)
        up.streams[0].end()
        await call.read_all()
        await settled_once(s, Outcome.OK, priced(s))


# --- the same through the buffered fake: every endpoint and adapter ---------------------------


@async_test
async def test_streaming_responses_messages_and_bedrock(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        user = [{"role": "user", "content": "hi"}]
        cases = (
            ("/v1/responses", {"model": "alpha", "input": "hi", "stream": True}, "event: "),
            ("/v1/messages", {"model": "haiku", "max_tokens": 50, "messages": user, "stream": True},
             "event: message_start"),
            (CHAT, {"model": "bedrock-haiku", "messages": user, "stream": True}, "data: "),
        )  # fmt: skip
        for path, body, marker in cases:
            r = await s.post(path, body)
            assert r.status_code == 200 and marker in r.text, (path, r.text[:200])
            assert r.headers["content-type"].startswith("text/event-stream")
            assert ": spendgate request_id=" in r.text and " cost=0." in r.text, path
        await s.pipeline.aclose()
        assert (await s.totals())[1] == 0
        assert [e.outcome for e in s.observer.settled] == [Outcome.OK] * 3
        assert len(s.observer.held) == len(s.observer.settled)


@async_test
async def test_responses_stream_cut_gets_an_error_event(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(cut_after_frames=2))
        r = await s.post("/v1/responses", {"model": "alpha", "input": "hi", "stream": True})
        assert "event: error" in r.text and '"sequence_number"' in r.text
        await s.pipeline.aclose()
        assert s.observer.settled[0].outcome is Outcome.STREAM_BROKEN
        assert (await s.totals())[0] == s.observer.held[0].hold.nano


@async_test
async def test_messages_stream_cut_gets_an_anthropic_error_event(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(cut_after_frames=2))
        body = {"model": "haiku", "max_tokens": 50, "stream": True}
        r = await s.post("/v1/messages", body | {"messages": [{"role": "user", "content": "x"}]})
        assert 'event: error\ndata: {"type":"error","error":' in r.text
        await s.pipeline.aclose()
        assert s.observer.settled[0].outcome is Outcome.STREAM_BROKEN


# --- before the first byte: unchanged classification ------------------------------------------


@async_test
async def test_pre_stream_429_fails_over_and_all_429_releases(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(status=429))  # deployment a: free, next candidate
        ok = await s.post(CHAT, BODY)
        assert ok.status_code == 200 and ": spendgate" in ok.text
        assert [r.headers["host"] for r in s.fake.requests] == [
            "a.upstream.test",
            "b.upstream.test",
        ]
        s.fake.enqueue(Script(status=429))
        s.fake.enqueue(Script(status=429))
        refused = await s.post(CHAT, BODY | {"model": "alpha3"})
        assert (
            refused.status_code == 429 and refused.json()["error"]["code"] == "upstream_throttled"
        )
        await s.pipeline.aclose()
        assert (await s.totals())[1] == 0
        assert s.observer.settled[1].released and s.observer.settled[1].charged.nano == 0


@async_test
async def test_pre_stream_5xx_is_json_and_charged_the_full_hold(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(status=503))
        r = await s.post(CHAT, BODY)
        assert r.status_code == 502 and r.headers["content-type"] == "application/json"
        assert r.headers["x-should-retry"] == "false"
        await s.pipeline.aclose()
        assert (await s.totals())[0] == s.observer.held[0].hold.nano


# --- hostile providers (C-22) ------------------------------------------------------------------


@async_test
async def test_malformed_sse_is_settled_at_the_hold_without_crashing(
    tmp_path: Path,
) -> None:  # T-PROV-02
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        up.streams[0].put(b"data: \xff\xfe not utf-8\n\n")
        await call.read_all()
        await call.finish()
        await settled_once(s, Outcome.STREAM_BROKEN, s.observer.held[0].hold.nano)


@async_test
async def test_endless_stream_is_cut_at_the_limit(tmp_path: Path) -> None:  # T-PROV-03
    limits = SseLimits(max_events=3)
    async with open_stack(tmp_path, pipeline_kwargs={"stream_limits": limits}) as s:
        s.fake.enqueue(Script(extra_events=50))
        r = await s.post(CHAT, BODY)
        assert '"error"' in r.text and r.text.count("data: ") < 10  # cut early, then error + DONE
        await s.pipeline.aclose()
        assert s.observer.settled[0].outcome is Outcome.STREAM_BROKEN
        assert (await s.totals())[0] == s.observer.held[0].hold.nano


@async_test
async def test_oversize_event_is_cut(tmp_path: Path) -> None:  # T-PROV-03
    limits = SseLimits(max_event_bytes=2_000)
    async with open_stack(tmp_path, pipeline_kwargs={"stream_limits": limits}) as s:
        s.fake.enqueue(Script(oversize_event_bytes=10_000))
        r = await s.post(CHAT, BODY)
        assert '"error"' in r.text
        await s.pipeline.aclose()
        assert s.observer.settled[0].outcome is Outcome.STREAM_BROKEN


@async_test
async def test_absurd_streamed_usage_is_booked_in_full_and_flagged(  # T-PROV-01, M-03
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async with open_stack(tmp_path, budget="1000000000") as s:
        s.fake.enqueue(Script(usage=ABSURD_USAGE))
        with caplog.at_level(logging.WARNING, logger="spendgate.pipeline"):
            r = await s.post(CHAT, BODY)
        assert r.status_code == 200
        await s.pipeline.aclose()
        event = s.observer.settled[0]
        assert event.review_required and event.charged.nano > 10 * event.hold.nano  # never clamped
        (row,) = await s.ledger.get_calls(event.request_id)
        assert row.review_required_at is not None and row.charged_nano == event.charged.nano
        assert "absurd provider usage" in caplog.text and event.request_id in caplog.text


@async_test
async def test_absurd_buffered_usage_is_flagged_too(tmp_path: Path) -> None:  # T-PROV-01
    async with open_stack(tmp_path, budget="1000000000") as s:
        s.fake.enqueue(Script(usage=ABSURD_USAGE))
        await s.chat()
        await s.pipeline.aclose()
        assert s.observer.settled[0].review_required
        (row,) = await s.ledger.get_calls(s.observer.settled[0].request_id)
        assert row.review_required_at is not None
        await s.chat()  # an honest call is not flagged
        assert not s.observer.settled[1].review_required


@async_test
async def test_settled_event_carries_remaining_for_the_gauge(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        r = await s.chat()
        event = s.observer.settled[0]
        assert event.remaining is not None and event.scope_path == "org:acme/user:u1"
        assert event.remaining.format() == r.headers["x-spendgate-budget-remaining"]
        assert event.ttft_ms is None  # buffered calls have no first-byte time
