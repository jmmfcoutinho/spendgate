"""Issue 18: one key verification per request, scope chains that expire, a supervised heartbeat.

`serve` verified the key and then `handle` verified it again with a later clock reading; the
closer kept every scope chain for the life of the process; and the heartbeat ended at the first
step that raised (the gateway) or swallowed it without a trace (library mode).
"""

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from spendgate.core.errors import Refusal
from spendgate.core.money import Money
from spendgate.ledger.key_types import Principal
from spendgate.ledger.port import LedgerPort, ScopeRow
from spendgate.ledger.sqlite import SqliteLedger
from spendgate.local.runtime import Runtime
from spendgate.pipeline.closing import CHAIN_TTL_MS
from spendgate.pipeline.model import KeyVerifier
from tests.conftest import FakeClock
from tests.integration.harness import NOW_MS, async_test, open_stack
from tests.integration.test_local_support import settings_in

_LOGGER = "spendgate.pipeline"


class CountingVerifier:
    """A `KeyVerifier` spy: counts verifications and passes them on."""

    def __init__(self, inner: KeyVerifier) -> None:
        self._inner = inner
        self.calls = 0

    async def verify_api(self, raw: str, now: int) -> Principal | Refusal:
        self.calls += 1
        return await self._inner.verify_api(raw, now)


@async_test
async def test_serve_verifies_the_key_once_per_request(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        spy = CountingVerifier(s.keys)
        s.pipeline._keys = spy
        assert (await s.chat()).status_code == 200
        assert spy.calls == 1
        assert (await s.chat()).status_code == 200
        assert spy.calls == 2


class MovableScopes:
    """A ledger whose `get_scope` reads are counted and whose parents a test can cut."""

    def __init__(self, inner: LedgerPort) -> None:
        self._inner = inner
        self.reads = 0
        self.detached: set[str] = set()  # scopes reported with no parent, as if re-parented

    async def get_scope(self, scope_id: str) -> ScopeRow | None:
        self.reads += 1
        row = await self._inner.get_scope(scope_id)
        if row is not None and row.id in self.detached:
            return dataclasses.replace(row, parent_id=None)
        return row

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


@async_test
async def test_a_cached_scope_chain_is_read_again_after_the_ttl(tmp_path: Path) -> None:
    holder: list[MovableScopes] = []

    def wrap(inner: SqliteLedger) -> LedgerPort:
        holder.append(MovableScopes(inner))
        return holder[0]  # type: ignore[return-value]

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
