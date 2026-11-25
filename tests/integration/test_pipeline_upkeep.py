"""Issue 18: one key verification per request, scope chains that expire, a supervised heartbeat.

`serve` verified the key and then `handle` verified it again with a later clock reading; the
closer kept every scope chain for the life of the process; and the heartbeat ended at the first
step that raised (the gateway) or swallowed it without a trace (library mode). One verification
remains, plus a cheap re-check after the upload that the same key is still active.
"""

import asyncio
import dataclasses
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.money import Money
from spendgate.ledger.key_types import KeySpec, Principal
from spendgate.ledger.keys import KeyService
from spendgate.ledger.port import MoneyLedger, ScopeRow
from spendgate.ledger.sqlite import SqliteLedger
from spendgate.local.runtime import Runtime
from spendgate.pipeline.closing import CHAIN_TTL_MS
from spendgate.pipeline.model import CHAT, KeyVerifier
from tests.conftest import FakeClock
from tests.fakes.ledger import MoneyLedgerProxy
from tests.integration.harness import NOW_MS, PEPPER, Stack, async_test, open_stack
from tests.integration.test_local_support import settings_in

_LOGGER = "spendgate.pipeline"


class CountingVerifier:
    """A `KeyVerifier` spy: counts verifications and re-checks and passes them on."""

    def __init__(self, inner: KeyVerifier) -> None:
        self._inner = inner
        self.calls = 0
        self.rechecks = 0

    async def verify_api(self, raw: str, now: int) -> Principal | Refusal:
        self.calls += 1
        return await self._inner.verify_api(raw, now)

    async def still_active(self, principal: Principal, now: int) -> bool:
        self.rechecks += 1
        return await self._inner.still_active(principal, now)


@async_test
async def test_serve_verifies_the_key_once_per_request(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        spy = CountingVerifier(s.keys)
        s.pipeline._keys = spy
        assert (await s.chat()).status_code == 200
        assert (spy.calls, spy.rechecks) == (1, 1)
        assert (await s.chat()).status_code == 200
        assert (spy.calls, spy.rechecks) == (2, 2)


class SlowUpload:
    """A request body that sends its first half, waits until the test opens `gate`, then ends."""

    def __init__(self, raw: bytes) -> None:
        half = len(raw) // 2
        self.parts = [raw[:half], raw[half:]]
        self.waiting, self.gate = asyncio.Event(), asyncio.Event()

    def __aiter__(self) -> "SlowUpload":
        return self

    async def __anext__(self) -> bytes:
        if len(self.parts) == 1:
            self.waiting.set()
            await self.gate.wait()
        if not self.parts:
            raise StopAsyncIteration
        return self.parts.pop(0)


async def upload_while(s: Stack, meanwhile: Callable[[str], Awaitable[object]]) -> tuple[int, str]:
    """Serve one chat whose upload stalls halfway while `meanwhile(key_id)` runs; (status, code)."""
    spy = CountingVerifier(s.keys)
    s.pipeline._keys = spy
    who = await s.keys.verify_api(s.secret, s.clock.now())
    assert isinstance(who, Principal)
    body = {"model": "alpha", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
    upload = SlowUpload(json.dumps(body).encode())
    headers = {"authorization": f"Bearer {s.secret}", "content-type": "application/json"}
    call = asyncio.ensure_future(s.pipeline.serve(CHAT, headers, upload))
    await asyncio.wait_for(upload.waiting.wait(), 5)
    await meanwhile(who.key_id)
    upload.gate.set()
    reply = await asyncio.wait_for(call, 5)
    assert (spy.calls, spy.rechecks) == (1, 1)  # one full verification, one cheap re-check
    return reply.status, json.loads(reply.body).get("error", {}).get("code", "")


@async_test
async def test_a_key_revoked_during_a_slow_upload_is_refused_before_the_hold(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path) as s:

        async def revoke(key_id: str) -> None:
            await s.keys.revoke_key(key_id, actor="test", now=s.clock.now())

        status, code = await upload_while(s, revoke)
        assert (status, code) == (401, ErrorCode.UNAUTHORIZED.value)
        s.assert_no_contact()
        assert await s.totals() == (0, 0)  # nothing was held


@async_test
async def test_a_key_that_expires_during_a_slow_upload_is_refused_before_the_hold(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path) as s:

        async def rotate_and_wait(key_id: str) -> None:
            await s.keys.rotate_key(key_id, grace_ms=1_000, actor="test", now=s.clock.now())
            s.clock.advance(ms=1_000)  # the old key's grace period ends while the body is sent

        status, code = await upload_while(s, rotate_and_wait)
        assert (status, code) == (401, ErrorCode.UNAUTHORIZED.value)
        s.assert_no_contact()
        assert await s.totals() == (0, 0)


@async_test
async def test_an_active_key_passes_the_re_check_after_a_slow_upload(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:

        async def nothing(_key_id: str) -> None:
            return None

        status, _code = await upload_while(s, nothing)
        assert status == 200


@async_test
async def test_still_active_reads_the_key_by_id_when_its_prefix_was_pushed_out(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path) as s:
        keys = KeyService(s.ledger, PEPPER, max_entries=1)
        who = await keys.verify_api(s.secret, NOW_MS)
        assert isinstance(who, Principal)
        other = await keys.create_key(KeySpec(s.scope.id, "other"), actor="test", now=NOW_MS)
        assert isinstance(await keys.verify_api(other.secret, NOW_MS), Principal)  # evicts `who`
        assert who.key_id not in keys._prefixes
        assert await keys.still_active(who, NOW_MS)
        await s.ledger.revoke_key(who.key_id, actor="test", now=NOW_MS)  # behind the cache's back
        assert not await keys.still_active(who, NOW_MS)


class MovableScopes(MoneyLedgerProxy):
    """A ledger whose `get_scope` reads are counted and whose parents a test can cut."""

    def __init__(self, inner: MoneyLedger) -> None:
        super().__init__(inner)
        self.reads = 0
        self.detached: set[str] = set()  # scopes reported with no parent, as if re-parented

    async def get_scope(self, scope_id: str) -> ScopeRow | None:
        self.reads += 1
        row = await self.inner.get_scope(scope_id)
        if row is not None and row.id in self.detached:
            return dataclasses.replace(row, parent_id=None)
        return row


@async_test
async def test_a_cached_scope_chain_is_read_again_after_the_ttl(tmp_path: Path) -> None:
    holder: list[MovableScopes] = []

    def wrap(inner: SqliteLedger) -> MoneyLedger:
        holder.append(MovableScopes(inner))
        return holder[0]

    async with open_stack(tmp_path, wrap_ledger=wrap) as s:
        ledger, closer = holder[0], s.pipeline._closer
        org = s.scope.parent_id
        assert await closer.chain_scope_ids(s.scope.id) == (s.scope.id, org)
        reads = ledger.reads
        ledger.detached.add(s.scope.id)  # the key's scope moves out from under the org
        s.clock.advance(ms=CHAIN_TTL_MS - 1)
        assert await closer.chain_scope_ids(s.scope.id) == (s.scope.id, org)  # still cached
        assert ledger.reads == reads
        s.clock.advance(ms=1)
        assert await closer.chain_scope_ids(s.scope.id) == (s.scope.id,)  # the move is seen
        assert ledger.reads > reads


def beats_then_stop(count: int) -> tuple[list[float], Callable[[float], Awaitable[None]]]:
    """A heartbeat `sleep` that returns at once `count` times, then cancels the loop."""
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        if len(slept) == count:
            raise asyncio.CancelledError
        slept.append(seconds)

    return slept, sleep


@async_test
async def test_a_heartbeat_step_that_raises_once_is_logged_once_and_the_loop_goes_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async with open_stack(tmp_path) as s:
        upkeep = s.pipeline._upkeep
        renew, renewals, watched = upkeep.renew_leases, [0], [0]

        async def flaky_renew() -> None:
            renewals[0] += 1
            if renewals[0] == 1:
                raise RuntimeError("ledger blinked")
            await renew()

        async def counted_watch() -> None:
            watched[0] += 1

        upkeep.renew_leases = flaky_renew  # type: ignore[method-assign]
        upkeep.watch_streams = counted_watch  # type: ignore[method-assign]
        slept, sleep = beats_then_stop(3)
        with caplog.at_level(logging.ERROR, logger=_LOGGER), pytest.raises(asyncio.CancelledError):
            await s.pipeline.heartbeat(sleep=sleep)
        assert slept == [s.settings.heartbeat_ms / 1000] * 3
        assert renewals == [3] and watched == [3]  # the failed step did not end the loop
        failures = [r for r in caplog.records if r.name == _LOGGER]
        assert len(failures) == 1
        assert "renew_leases" in failures[0].getMessage()
        assert "RuntimeError" in failures[0].getMessage()
        assert "ledger blinked" not in failures[0].getMessage()  # the type only, never the text


@async_test
async def test_library_mode_runs_the_supervised_heartbeat_and_logs_a_failed_sweep(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NOW_MS)
    runtime = Runtime(
        settings_in(tmp_path), tmp_path / "local.db", Money.from_amount("10", "USD"),
        now_ms=clock.now,
    )  # fmt: skip
    try:
        pipeline = await runtime.start()
        sweep, sweeps = runtime._ledger.sweep_expired, [0]

        async def flaky_sweep(now: int, limit: int | None = None) -> object:
            sweeps[0] += 1
            if sweeps[0] == 1:
                raise RuntimeError("ledger blinked")
            return await sweep(now)

        monkeypatch.setattr(runtime._ledger, "sweep_expired", flaky_sweep)
        _slept, sleep = beats_then_stop(2)
        with caplog.at_level(logging.ERROR, logger=_LOGGER), pytest.raises(asyncio.CancelledError):
            await runtime._maintain(pipeline, sleep=sleep)
        assert sweeps == [2]
        failures = [r for r in caplog.records if r.name == _LOGGER]
        assert len(failures) == 1 and "sweep" in failures[0].getMessage()
    finally:
        await runtime.aclose()


async def no_wait(_seconds: float) -> None:
    return None


@async_test
async def test_cancelling_the_heartbeat_while_a_step_is_blocked_stops_it(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        entered, never = asyncio.Event(), asyncio.Event()

        async def blocked() -> None:
            entered.set()
            await never.wait()

        beat = asyncio.ensure_future(s.pipeline.heartbeat(no_wait, also=[("blocked", blocked)]))
        await asyncio.wait_for(entered.wait(), 5)
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
        assert beat.cancelled()


@async_test
async def test_a_step_that_raises_cancelled_error_by_itself_does_not_end_the_heartbeat(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async with open_stack(tmp_path) as s:
        runs = [0]

        async def stray_cancel() -> None:
            runs[0] += 1
            if runs[0] == 1:
                raise asyncio.CancelledError  # an inner await was cancelled, not the heartbeat

        _slept, sleep = beats_then_stop(3)
        with caplog.at_level(logging.ERROR, logger=_LOGGER), pytest.raises(asyncio.CancelledError):
            await s.pipeline.heartbeat(sleep, also=[("stray", stray_cancel)])
        assert runs == [3]
        failures = [r for r in caplog.records if r.name == _LOGGER]
        assert len(failures) == 1 and "CancelledError" in failures[0].getMessage()
