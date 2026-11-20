"""Fixes from the v0.1 verification review (docs/reviews/verification-v0.1.md): NF-01 to NF-04,
the rest of SR-07 and the CR-07/CR-08 nits. Each test fails on the code before its fix."""

import asyncio
import json
from pathlib import Path

import pytest

from spendgate.client.errors import from_sdk_error
from spendgate.core.errors import ErrorCode
from spendgate.core.outcomes import Outcome, ReservationState
from spendgate.core.policy import KillSwitch, KillTarget
from spendgate.gateway.metrics import Metrics
from spendgate.ledger.key_types import KeySpec
from spendgate.ledger.port import LedgerPort, LedgerUnavailable
from spendgate.ledger.sqlite import SqliteLedger
from spendgate.pipeline import intake
from spendgate.pipeline.model import CHAT
from tests.integration import test_gateway_streaming as streaming_tests
from tests.integration.harness import NOW_MS, Stack, async_test, open_stack
from tests.integration.test_admin import BASE
from tests.integration.test_admin_support import open_admin
from tests.integration.test_gateway_streaming import gated

MIB = 1024 * 1024
JSON_HEAD = {"content-type": "application/json"}


class Upload:
    """A request body that sends `parts`, then stalls forever (or ends)."""

    def __init__(self, parts: list[bytes], *, stall: bool = True) -> None:
        self.parts, self.stall = list(parts), stall

    def __aiter__(self) -> "Upload":
        return self

    async def __anext__(self) -> bytes:
        if self.parts:
            return self.parts.pop(0)
        if self.stall:
            await asyncio.Event().wait()
        raise StopAsyncIteration


async def started(body: Upload) -> None:
    while body.parts:  # every part was pulled: the read is now stalled
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)


async def second_key(s: Stack) -> str:
    made = await s.keys.create_key(KeySpec(s.scope.id, "other"), actor="test", now=NOW_MS)
    return made.secret


def auth(secret: str) -> dict[str, str]:
    return {"authorization": f"Bearer {secret}"} | JSON_HEAD


# --- NF-01 -------------------------------------------------------------------------------------


def test_nf_01_an_honest_request_is_not_charged_the_worst_case_before_its_bytes_arrive() -> None:
    gate = intake.Gate(8, 4 * MIB, 384 * MIB)
    slot = gate.enter(None, max_body=4 * MIB, copies=intake.JSON_AMPLIFICATION, owner="k")
    assert isinstance(slot, intake.Slot)
    assert gate._bytes == 0  # nothing buffered yet, nothing charged (was 4 MiB x 32)
    assert slot.grow(1 * MIB) is None
    assert gate._bytes == MIB * intake.JSON_AMPLIFICATION
    slot.release()
    assert gate._bytes == 0 and gate.in_flight == 0


def test_nf_01_one_key_cannot_take_more_than_its_share_of_the_gate() -> None:
    gate = intake.Gate(8, 4 * MIB, 384 * MIB)
    a = gate.enter(None, max_body=4 * MIB, copies=32, owner="a")
    b = gate.enter(None, max_body=4 * MIB, copies=32, owner="a")
    other = gate.enter(None, max_body=4 * MIB, copies=32, owner="b")
    assert isinstance(a, intake.Slot) and isinstance(b, intake.Slot)
    assert isinstance(other, intake.Slot)
    assert a.grow(3 * MIB) is None  # 96 MiB: the key's quarter
    refusal = b.grow(MIB)  # a second upload of the same key: over its share
    assert refusal is not None and refusal.code is ErrorCode.RATE_LIMITED
    assert other.grow(3 * MIB) is None  # another key is untouched
    assert gate._bytes == 2 * 96 * MIB


def test_nf_01_a_key_can_always_have_one_maximal_request() -> None:
    gate = intake.Gate(8, 4 * MIB, 384 * MIB)
    slot = gate.enter(4 * MIB, max_body=4 * MIB, copies=32, owner="a")  # 128 MiB > a quarter
    assert isinstance(slot, intake.Slot)


@async_test
async def test_nf_01_stalled_uploads_of_one_key_do_not_lock_out_another_key(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path, max_concurrent=16) as s:
        other = await second_key(s)
        stalled = [Upload([b" " * 3 * MIB]) for _ in range(5)]
        calls = [
            asyncio.ensure_future(s.pipeline.serve(CHAT, auth(s.secret), body)) for body in stalled
        ]
        for body in stalled:
            await started(body)
        assert s.pipeline.gate._bytes <= s.pipeline.gate.memory_bytes // 4
        reply = await s.post("/v1/chat/completions", {
            "model": "alpha", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}],
        }, key=other)  # fmt: skip
        assert reply.status_code == 200
        for call in calls:
            call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)
        assert s.pipeline.gate.in_flight == 0 and s.pipeline.gate._bytes == 0


@async_test
async def test_nf_01_three_chunked_json_uploads_no_longer_fill_the_gate(tmp_path: Path) -> None:
    async with open_stack(tmp_path, max_concurrent=16) as s:
        other = await second_key(s)
        bodies = [Upload([b"{"]) for _ in range(3)]
        calls = [asyncio.ensure_future(s.pipeline.serve(CHAT, auth(s.secret), b)) for b in bodies]
        for body in bodies:
            await started(body)
        assert s.pipeline.gate._bytes < MIB  # the old charge was 3 x 128 MiB
        ok = await s.post("/v1/chat/completions", {
            "model": "alpha", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}],
        }, key=other)  # fmt: skip
        assert ok.status_code == 200
        for call in calls:
            call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)


# --- NF-02 -------------------------------------------------------------------------------------


async def open_stream(s: Stack) -> object:
    raw = json.dumps(streaming_tests.BODY).encode()
    reply = await s.pipeline.serve(CHAT, auth(s.secret), Upload([raw], stall=False))
    assert reply.status == 200 and reply.stream is not None
    return reply.stream


async def until_settled(s: Stack, rid: str) -> None:
    for _ in range(400):
        res = await s.ledger.get_reservation(rid)
        if res is not None and res.state is not ReservationState.PENDING:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the hold was never settled")


@async_test
async def test_nf_02_a_stream_nobody_reads_is_stopped_at_its_deadline_and_settled(
    tmp_path: Path,
) -> None:
    async with gated(tmp_path, pipeline_kwargs={"max_request_seconds": 120}) as (s, _up):
        body = await open_stream(s)
        rid = s.observer.held[0].request_id
        for _ in range(8):  # 20 s heartbeats, 160 s: the stream is never iterated
            s.clock.advance(seconds=20)
            await s.pipeline.renew_leases()
            await s.pipeline.watch_streams()
        await until_settled(s, rid)
        res = await s.ledger.get_reservation(rid)
        assert res is not None and res.state is ReservationState.SETTLED
        held = s.observer.held[0].hold.nano
        assert await s.totals() == (held, 0)  # no usage seen: the full hold
        (row,) = await s.ledger.get_calls(rid)
        assert row.outcome is Outcome.STREAM_BROKEN
        assert rid not in s.pipeline._owned
        await body.aclose()  # type: ignore[attr-defined]


@async_test
async def test_nf_02_a_kill_switch_settles_a_stream_nobody_reads(tmp_path: Path) -> None:
    async with gated(tmp_path) as (s, _up):
        body = await open_stream(s)
        rid = s.observer.held[0].request_id
        await s.ledger.engage_kill_switch(
            KillSwitch(KillTarget.GLOBAL), actor="t", reason="test", now=s.clock.now()
        )
        await s.pipeline.renew_leases()
        await s.pipeline.watch_streams()
        await until_settled(s, rid)
        (row,) = await s.ledger.get_calls(rid)
        assert row.outcome is Outcome.STREAM_BROKEN
        await body.aclose()  # type: ignore[attr-defined]


# --- NF-03 -------------------------------------------------------------------------------------


class HoldThenFail:
    """A ledger whose `hold` commits and then loses the reply (an ambiguous COMMIT)."""

    def __init__(self, inner: LedgerPort) -> None:
        self._inner = inner
        self.release_down = False

    async def hold(self, request: object) -> object:
        await self._inner.hold(request)  # type: ignore[arg-type]
        raise LedgerUnavailable("commit reply lost")

    async def release(self, *args: object) -> object:
        if self.release_down:
            raise LedgerUnavailable("down")
        return await self._inner.release(*args)  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def hold_then_fail(inner: SqliteLedger) -> LedgerPort:
    return HoldThenFail(inner)  # type: ignore[return-value]


async def beats(s: Stack, count: int) -> None:
    for _ in range(count):
        s.clock.advance(seconds=20)
        await s.pipeline.renew_leases()


@async_test
async def test_nf_03_a_hold_whose_commit_reply_was_lost_ends_free(tmp_path: Path) -> None:
    async with open_stack(tmp_path, wrap_ledger=hold_then_fail) as s:
        reply = await s.chat()
        assert reply.status_code == 503
        s.assert_no_contact()
        await beats(s, 5)
        await s.ledger.sweep_expired(s.clock.now())
        assert await s.totals() == (0, 0)  # released, never swept at the full hold


@async_test
async def test_nf_03_a_failed_release_is_retried_by_the_heartbeat(tmp_path: Path) -> None:
    async with open_stack(tmp_path, wrap_ledger=hold_then_fail) as s:
        s.pipeline._ledger.release_down = True  # type: ignore[attr-defined]
        assert (await s.chat()).status_code == 503
        assert len(s.pipeline._closer.unsettled()) == 1
        s.pipeline._ledger.release_down = False  # type: ignore[attr-defined]
        await s.pipeline.renew_leases()  # the retry runs before the lease is up
        await s.ledger.sweep_expired(s.clock.now() + 10 * 60 * 1000)
        assert await s.totals() == (0, 0)


class GiveBackLedger(HoldThenFail):
    """`hold` commits slowly so that the caller can be cancelled while it runs."""

    def __init__(self, inner: LedgerPort) -> None:
        super().__init__(inner)
        self.gate = asyncio.Event()

    async def hold(self, request: object) -> object:
        await self.gate.wait()
        await self._inner.hold(request)  # type: ignore[arg-type]
        raise LedgerUnavailable("commit reply lost")


@async_test
async def test_nf_03_give_back_after_a_cancelled_ambiguous_hold_ends_free(tmp_path: Path) -> None:
    holder: list[GiveBackLedger] = []

    def wrap(inner: SqliteLedger) -> LedgerPort:
        holder.append(GiveBackLedger(inner))
        return holder[0]  # type: ignore[return-value]

    async with open_stack(tmp_path, wrap_ledger=wrap) as s:
        call = asyncio.ensure_future(s.chat())
        for _ in range(50):
            await asyncio.sleep(0)
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        holder[0].gate.set()
        for _ in range(100):
            await asyncio.sleep(0.005)
            if await s.totals() == (0, 0) and not s.pipeline._tasks:
                break
        await beats(s, 5)
        await s.ledger.sweep_expired(s.clock.now())
        assert await s.totals() == (0, 0)


# --- NF-04 -------------------------------------------------------------------------------------


@async_test
async def test_nf_04_a_stream_close_that_raises_still_settles_and_is_no_longer_renewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise TypeError("injected")

    async with gated(tmp_path) as (s, _up):
        body = await open_stream(s)
        rid = s.observer.held[0].request_id
        monkeypatch.setattr("spendgate.pipeline.streaming.read_usage", boom)
        with pytest.raises(TypeError):
            await body.aclose()  # type: ignore[attr-defined]
        res = await s.ledger.get_reservation(rid)
        assert res is not None and res.state is ReservationState.SETTLED
        assert rid not in s.pipeline._owned
        assert await s.totals() == (s.observer.held[0].hold.nano, 0)  # a flight was open: full hold


# --- SR-07 (admin times) -----------------------------------------------------------------------

OVERFLOW = "0001-01-01T00:00:00+14:00"


@async_test
async def test_sr_07_an_overflowing_time_is_a_400_not_a_500(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        from tests.integration.test_admin import PATH  # noqa: PLC0415

        err = await a.error(
            "POST", f"{BASE}/keys", 400,
            json={"scope_id": PATH, "name": "k", "expires_at": OVERFLOW},
        )  # fmt: skip
        assert err["code"] == "invalid_request"
        err = await a.error("GET", f"{BASE}/usage", 400, params={"since": OVERFLOW})
        assert err["code"] == "invalid_request"


@async_test
async def test_sr_07_grant_times_that_overflow_are_a_400(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        from tests.integration.test_admin import PATH  # noqa: PLC0415

        made = await a.ok(
            "POST", f"{BASE}/scopes/{PATH}/budgets", 201,
            json={"cap": {"amount": "1", "currency": "USD"}},
        )  # fmt: skip
        grants = f"{BASE}/budgets/{made['id']}/grants"
        money = {"amount": "1", "currency": "USD"}
        for field in ("valid_from", "valid_until"):
            body = {
                "amount": money,
                "valid_from": "2031-01-01T00:00:00Z",
                "valid_until": "2031-02-01T00:00:00Z",
                "reason": "r",
            } | {field: OVERFLOW}
            assert (await a.error("POST", grants, 400, json=body))["code"] == "invalid_request"


# --- CR-07, CR-08 nits --------------------------------------------------------------------------


def test_cr_07_an_unknown_catalog_age_is_not_published_as_fresh() -> None:
    metrics = Metrics()
    metrics.set_catalog(sha="abc", age_seconds=None)
    assert "\nspendgate_catalog_age_seconds " not in metrics.render()
    metrics.set_catalog(sha="abc", age_seconds=5)
    assert "\nspendgate_catalog_age_seconds 5\n" in metrics.render()
    metrics.set_catalog(sha="abc", age_seconds=None)  # and it goes away again
    assert "\nspendgate_catalog_age_seconds " not in metrics.render()


class _SdkError(Exception):
    """What the openai/anthropic SDKs raise: duck-typed, with a response carrying headers."""

    def __init__(self, retry_after: str) -> None:
        super().__init__("rate limited")
        self.status_code = 429
        self.body = {"error": {"code": "rate_limited", "message": "m", "details": {}}}
        self.response = type("Response", (), {"headers": {"retry-after": retry_after}})()


HOSTILE = ["²", "1e999", "5" * 5000, "Wed, 21 Oct 2026 07:28:00 GMT", "-1", ""]


@pytest.mark.parametrize("raw", HOSTILE)
def test_cr_08_the_client_never_raises_on_a_hostile_retry_after(raw: str) -> None:
    typed = from_sdk_error(_SdkError(raw))
    assert typed is not None and typed.details.get("retry_after_seconds") is None


def test_cr_08_a_plain_retry_after_is_still_read() -> None:
    typed = from_sdk_error(_SdkError("7"))
    assert typed is not None and typed.details.get("retry_after_seconds") == 7
