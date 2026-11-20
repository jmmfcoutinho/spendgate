"""Fixes from the v0.1 reviews (CR-01/02/04/05/06, SR-01/02/07/13/14): one test per finding.

The money rules: a call that was sent is never unbooked, a hold its owner could not close reaches
the sweeper, and a refusal never reaches the provider.
"""

import ast
import asyncio
import json
import tracemalloc
from pathlib import Path

import httpx
import pytest

from spendgate.core.cost import Usage
from spendgate.core.errors import ErrorCode
from spendgate.core.outcomes import Outcome, ReservationState
from spendgate.ledger.port import LedgerPort, LedgerUnavailable
from spendgate.ledger.sqlite import SqliteLedger
from spendgate.pipeline import steps
from spendgate.pipeline.model import CHAT
from spendgate.providers.openai import OpenAIProvider
from tests.fakes import Script
from tests.integration.harness import Stack, async_test, open_stack
from tests.integration.stream_rig import DONE, chat_chunk
from tests.integration.test_gateway_streaming import BODY as STREAM_BODY
from tests.integration.test_gateway_streaming import gated, open_call, settled_once

MIB = 1024 * 1024
SRC = Path(__file__).resolve().parents[2] / "src" / "spendgate"


async def no_sleep(seconds: float) -> None:
    return None


class SettleDown:
    """A ledger whose `settle` fails (the database died after the provider answered)."""

    def __init__(self, inner: LedgerPort) -> None:
        self._inner = inner
        self.down = True

    async def settle(self, *args: object) -> object:
        if self.down:
            raise LedgerUnavailable("gone")
        return await self._inner.settle(*args)  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def settle_down(inner: SqliteLedger) -> LedgerPort:
    return SettleDown(inner)  # type: ignore[return-value]  # a partial stand-in, like the others


def raises(*args: object, **kwargs: object) -> object:
    raise RuntimeError("adapter bug")


async def booked_at_the_hold(s: Stack) -> None:
    held = s.observer.held[0]
    assert await s.totals() == (held.hold.nano, 0)
    (row,) = await s.ledger.get_calls(held.request_id)
    assert row.charged_nano == held.hold.nano and row.outcome is Outcome.INTERNAL_ERROR


# --- CR-01 -------------------------------------------------------------------------------------


@async_test
async def test_cr_01_usage_that_raises_after_send_is_charged_the_full_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(OpenAIProvider, "usage", raises)
    async with open_stack(tmp_path) as s:
        r = await s.chat()
        assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
        await booked_at_the_hold(s)


@async_test
async def test_cr_01_error_that_raises_after_send_is_charged_the_full_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(OpenAIProvider, "error", raises)
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(status=500))
        r = await s.chat()
        assert r.status_code == 500
        await booked_at_the_hold(s)


@async_test
async def test_cr_01_streamed_usage_that_raises_is_charged_the_full_hold(tmp_path: Path) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        up.streams[0].put(chat_chunk("a"))
        assert await call.chunk() is not None

        def boom() -> object:
            raise RuntimeError("relay bug")

        s.pipeline._leases.open()[0]._live.relay.usage = boom  # type: ignore[attr-defined]
        up.streams[0].put(DONE)
        up.streams[0].end()
        await call.read_all()
        await call.finish()
        await settled_once(s, Outcome.OK_NO_USAGE, s.observer.held[0].hold.nano)


# --- CR-02 -------------------------------------------------------------------------------------


@async_test
async def test_cr_02_heartbeat_after_a_failed_settle_lets_the_sweeper_charge_the_full_hold(
    tmp_path: Path,
) -> None:
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    async with open_stack(
        tmp_path,
        wrap_ledger=settle_down,
        pipeline_kwargs={"sleep": record},
    ) as s:
        r = await s.chat()
        rid = r.headers["x-spendgate-request-id"]
        held = s.observer.held[0].hold.nano
        assert sleeps == [0.05, 0.25]  # backoff between the three tries
        assert s.pipeline._closer.unsettled() == {rid}
        for _ in range(4):  # four 20 s heartbeats: the hold must not be kept alive
            s.clock.advance(seconds=20)
            await s.pipeline.renew_leases()
        swept = await s.ledger.sweep_expired(s.clock.now())
        assert swept.expired == (rid,)
        assert await s.totals() == (held, 0)


@async_test
async def test_cr_02_an_unsettled_close_is_retried_by_the_heartbeat(tmp_path: Path) -> None:
    async with open_stack(
        tmp_path,
        wrap_ledger=settle_down,
        pipeline_kwargs={"sleep": no_sleep},
    ) as s:
        r = await s.chat()
        rid = r.headers["x-spendgate-request-id"]
        s.pipeline._ledger.down = False  # type: ignore[attr-defined]  # the database is back
        await s.pipeline.renew_leases()
        assert s.pipeline._closer.unsettled() == frozenset()
        spent, held = await s.totals()
        assert held == 0 and spent == 3500  # settled at the usage, no sweeper involved
        res = await s.ledger.get_reservation(rid)
        assert res is not None and res.state is ReservationState.SETTLED


# --- CR-04 -------------------------------------------------------------------------------------


@async_test
async def test_cr_04_usage_that_does_not_fit_the_ledger_charges_the_hold_and_flags_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        OpenAIProvider, "usage", lambda self, req, resp: Usage(input_tokens=1, output_tokens=10**17)
    )
    async with open_stack(tmp_path) as s:
        r = await s.chat()
        held = s.observer.held[0]
        assert r.status_code == 200
        assert await s.totals() == (held.hold.nano, 0)  # booked, not left pending
        (row,) = await s.ledger.get_calls(held.request_id)
        assert row.review_required_at is not None and row.charged_nano == held.hold.nano
        res = await s.ledger.get_reservation(held.request_id)
        assert res is not None and res.state is ReservationState.SETTLED


# --- CR-05 -------------------------------------------------------------------------------------


@async_test
async def test_cr_05_no_cost_header_when_the_ledger_did_not_book(tmp_path: Path) -> None:
    async with open_stack(
        tmp_path,
        wrap_ledger=settle_down,
        pipeline_kwargs={"sleep": no_sleep},
    ) as s:
        r = await s.chat()
        assert r.status_code == 200 and "x-spendgate-cost" not in r.headers


# --- CR-06 -------------------------------------------------------------------------------------

QUOTA = {"error": {"message": "quota", "type": "insufficient_quota", "code": "insufficient_quota"}}


@async_test
async def test_cr_06_quota_on_every_attempt_is_upstream_unavailable_without_retry_hints(
    tmp_path: Path,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(429, json=QUOTA)

    sender = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with open_stack(tmp_path, pipeline_kwargs={"sender": sender}) as s:
        r = await s.chat()
        assert len(seen) == 2  # the free failover still happens
        assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_unavailable"
        assert "retry-after" not in r.headers and r.headers["x-should-retry"] == "false"
        assert "retry_after_seconds" not in r.json()["error"].get("details", {})
        assert await s.totals() == (0, 0)  # free
        again = await s.chat()  # both deployments are cooling on a quota fault
        assert again.status_code == 502 and again.json()["error"]["code"] == "upstream_unavailable"
    await sender.aclose()


# --- SR-01, SR-02 ------------------------------------------------------------------------------


class Body:
    """A request body that counts what was pulled from it, optionally stalling forever."""

    def __init__(self, parts: list[bytes], *, stall: bool = False) -> None:
        self.parts, self.stall, self.pulled = parts, stall, 0

    def __aiter__(self) -> "Body":
        return self

    async def __anext__(self) -> bytes:
        if self.parts:
            part = self.parts.pop(0)
            self.pulled += len(part)
            return part
        if self.stall:
            await asyncio.Event().wait()
        raise StopAsyncIteration


def bearer(s: Stack) -> dict[str, str]:
    return {"authorization": f"Bearer {s.secret}", "content-type": "application/json"}


# T-DOS-01
@async_test
async def test_sr_01_an_unauthenticated_25_mib_json_body_is_refused_unread(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        body = Body([b"[" + b"{}," * 8 * MIB])  # about 25 MiB
        headers = {"content-type": "application/json", "content-length": str(25 * MIB)}
        reply = await s.pipeline.serve(CHAT, headers, body)
        assert reply.status == 401 and body.pulled == 0
        assert s.pipeline.gate.in_flight == 0
        s.assert_no_contact()


# T-DOS-01, T-DOS-03
@async_test
async def test_sr_01_json_has_its_own_small_cap_checked_before_the_read(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        body = Body([b"x"])
        headers = bearer(s) | {"content-length": str(5 * MIB)}  # over 4 MiB, under 25 MiB
        reply = await s.pipeline.serve(CHAT, headers, body)
        assert reply.status == 413 and body.pulled == 0
        chunked = Body([b" " * (4 * MIB), b" "])  # no Content-Length: the read cap stops it
        assert (await s.pipeline.serve(CHAT, bearer(s), chunked)).status == 413


def test_sr_01_default_gate_memory_stays_under_the_reference_container_limit() -> None:
    from spendgate.pipeline import intake  # noqa: PLC0415

    assert intake.DEFAULT_MEMORY_BYTES <= 400 * MIB < 512 * MIB


@pytest.mark.slow
@async_test
async def test_sr_01_parallel_amplifying_json_bodies_stay_under_the_gate_budget(
    tmp_path: Path,
) -> None:
    head = b'{"model":"alpha","max_tokens":10,"messages":[{"role":"user","content":"hi"}],"x":['
    body = head + b"{}," * (4 * MIB // 3 - 100) + b"{}]}"
    assert len(body) < 4 * MIB
    async with open_stack(tmp_path) as s:
        budget = s.pipeline.gate.memory_bytes
        headers = {"Authorization": f"Bearer {s.secret}", "Content-Type": "application/json"}
        tracemalloc.start()
        tracemalloc.reset_peak()
        try:
            responses = await asyncio.gather(
                *[s.client.post(CHAT.path, content=body, headers=headers) for _ in range(8)]
            )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < budget
        assert {r.status_code for r in responses} <= {200, 400, 429}


@async_test
async def test_sr_01_per_key_uploads_are_limited_before_the_gate_and_freed_after_the_read(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path, max_concurrent=1) as s:
        slow = Body([b"{"], stall=True)
        first = asyncio.ensure_future(s.pipeline.serve(CHAT, bearer(s), slow))
        while slow.pulled == 0:
            await asyncio.sleep(0)
        second = Body([b"{}"])
        reply = await s.pipeline.serve(CHAT, bearer(s), second)
        assert reply.status == 429 and second.pulled == 0
        assert json.loads(reply.body)["error"]["details"]["limit"] == "concurrency"
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        ok = await s.chat()  # the ticket came back, and the real limit has room
        assert ok.status_code == 200


# T-DOS-04
@async_test
async def test_sr_02_a_stalled_body_is_refused_with_408_and_frees_its_place(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path, pipeline_kwargs={"body_timeout_seconds": 0.05}) as s:
        reply = await s.pipeline.serve(CHAT, bearer(s), Body([b"{"], stall=True))
        assert reply.status == 408 and json.loads(reply.body)["error"]["code"] == "invalid_request"
        assert s.pipeline.gate.in_flight == 0
        s.assert_no_contact()


# T-DOS-04
@async_test
async def test_sr_02_an_unauthenticated_stalled_body_never_holds_a_place(tmp_path: Path) -> None:
    async with open_stack(tmp_path, concurrent=1) as s:
        stalled = Body([b"{"], stall=True)
        reply = await s.pipeline.serve(CHAT, {"content-type": "application/json"}, stalled)
        assert reply.status == 401 and stalled.pulled == 0 and s.pipeline.gate.in_flight == 0


# --- SR-07 -------------------------------------------------------------------------------------


@async_test
async def test_sr_07_deeply_nested_json_is_invalid_request_not_a_500(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        deep = b"[" * 200_000 + b"]" * 200_000
        r = await s.client.post(CHAT.path, content=deep, headers=bearer(s))
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"


@async_test
async def test_sr_07_arithmetic_error_in_estimation_is_invalid_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import decimal  # noqa: PLC0415

    def overflow(*args: object, **kwargs: object) -> object:
        raise decimal.Overflow("too big")

    monkeypatch.setattr(steps, "estimate", overflow)
    async with open_stack(tmp_path) as s:
        r = await s.chat()
        assert r.status_code == 400 and r.json()["error"]["code"] == ErrorCode.INVALID_REQUEST
        s.assert_no_contact()


# --- SR-13 -------------------------------------------------------------------------------------


@async_test
async def test_sr_13_a_call_that_runs_too_long_is_timed_out_at_the_full_hold(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path, pipeline_kwargs={"max_request_seconds": 0.05}) as s:
        s.fake.enqueue(Script(hang=True))
        r = await s.chat()
        assert r.status_code == 504 and r.json()["error"]["code"] == "upstream_unavailable"
        held = s.observer.held[0]
        assert await s.totals() == (held.hold.nano, 0)
        (row,) = await s.ledger.get_calls(held.request_id)
        assert row.outcome is Outcome.TIMEOUT


@async_test
async def test_sr_13_a_stream_that_runs_too_long_is_broken_with_usage_if_seen_else_the_hold(
    tmp_path: Path,
) -> None:
    async with gated(tmp_path, pipeline_kwargs={"max_request_seconds": 120}) as (s, up):
        call = await open_call(s, STREAM_BODY)
        for _ in range(4):  # 30 s beats keep the lease alive; the stream is at 120 s
            s.clock.advance(seconds=30)
            await s.pipeline.renew_leases()
        up.streams[0].put(chat_chunk("late"))
        text = (await call.read_all()).decode()
        assert '"error"' in text
        await call.finish()
        held = s.observer.held[0]
        (row,) = await s.ledger.get_calls(held.request_id)
        assert row.outcome is Outcome.STREAM_BROKEN
        assert "longer than the gateway allows" in text
        await settled_once(s, Outcome.STREAM_BROKEN, held.hold.nano)


# --- SR-14 -------------------------------------------------------------------------------------


@pytest.mark.parametrize("module", ["ledger/hold_flow.py", "pipeline/settle.py"])
def test_sr_14_state_machine_invariants_are_not_guarded_by_assert(module: str) -> None:
    tree = ast.parse((SRC / module).read_text(encoding="utf-8"))
    assert not [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assert)]
