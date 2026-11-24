"""Runaway loops (TH-09, C-18): a loop that keeps calling is stopped by its budget or a kill switch.

T-SESS-02 runs many parallel loops on one key until each is refused, on SQLite and on Postgres:
the cap holds under contention and nothing stays held. T-SESS-03 engages a kill switch while a
loop runs: the next call is refused and a stream still open stops at the next heartbeat. The clock
is the injected `FakeClock` and the heartbeat is driven by hand, never by sleeping.
"""

import asyncio
import json
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from spendgate.core.budgets import Period, ScopeKind
from spendgate.core.policy import KillTarget
from spendgate.ledger.engine import LedgerEngine
from spendgate.ledger.key_types import KeySpec
from spendgate.ledger.keys import KeyService
from spendgate.ledger.postgres import PostgresLedger
from tests.contract.rig import workdir_location
from tests.fakes import FakeUpstream, FakeUsage, Script
from tests.integration.harness import CHAT, NOW_MS, PEPPER, Stack, async_test, open_stack, usd
from tests.integration.stream_rig import chat_chunk, chat_usage
from tests.integration.test_gateway_stream_stops import beat, engage
from tests.integration.test_gateway_streaming import SEEN, gated, open_call, priced

WORKERS = 40
CAP = "0.01"
# Each call uses its whole `max_tokens`, so its cost is close to its hold: an admission that let
# concurrent holds through would show as spend over the cap, not hide in the gap to the hold.
FULL = FakeUsage(output_tokens=100)
MAX_LOOPS = 100  # per worker; reaching it means the budget never stopped the loop
LOOP_BODY: dict[str, object] = {
    "model": "alpha", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}],
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class Books:
    """Where the loop's money lives: the ledger the pipeline books on, and the key's budget."""

    ledger: LedgerEngine
    scope_id: str
    budget_id: str
    secret: str

    async def totals(self) -> tuple[int, int]:
        rows = await self.ledger.totals(self.scope_id, NOW_MS)
        row = next(r for r in rows if r.budget_id == self.budget_id)
        return row.spent_nano, row.held_nano


def full_upstream() -> FakeUpstream:
    return FakeUpstream(Script(usage=FULL))


@asynccontextmanager
async def sqlite_books(tmp_path: Path) -> AsyncIterator[tuple[Stack, Books]]:
    async with open_stack(tmp_path, budget=CAP, fake=full_upstream()) as s:
        yield s, Books(s.ledger, s.scope.id, s.budget.id, s.secret)


@asynccontextmanager
async def postgres_books(tmp_path: Path) -> AsyncIterator[tuple[Stack, Books]]:
    """The same stack with the pipeline and key checks on Postgres (its SQLite ledger idles)."""
    ledger = await PostgresLedger.open(workdir_location("postgres", tmp_path), currency="USD")
    try:
        keys = KeyService(ledger, PEPPER)
        org = await ledger.create_scope(ScopeKind.ORG, "acme", None, now=NOW_MS)
        scope = await ledger.create_scope(ScopeKind.USER, "u1", org.id, now=NOW_MS)
        budget = await ledger.create_budget(scope.id, Period.NONE, usd(CAP), now=NOW_MS)
        made = await keys.create_key(KeySpec(scope.id, "loop"), actor="test", now=NOW_MS)
        swap = {"ledger": ledger, "keys": keys}
        async with open_stack(tmp_path, fake=full_upstream(), pipeline_kwargs=swap) as s:
            yield s, Books(ledger, scope.id, budget.id, made.secret)
    finally:
        await ledger.aclose()


async def run_away(s: Stack, secret: str) -> tuple[int, Counter[str]]:
    """WORKERS loops on one key, each calling until its first refusal: (calls served, codes)."""
    served = 0
    codes: Counter[str] = Counter()

    async def loop() -> None:
        nonlocal served
        for _ in range(MAX_LOOPS):
            r = await s.post(CHAT, LOOP_BODY, key=secret)
            if r.status_code != 200:
                codes[r.json()["error"]["code"]] += 1
                return
            served += 1
        raise AssertionError("the budget never stopped the loop")

    await asyncio.gather(*(loop() for _ in range(WORKERS)))
    return served, codes


async def check_runaway_stops_at_the_cap(s: Stack, books: Books) -> None:
    served, codes = await run_away(s, books.secret)
    spent, held = await books.totals()
    assert codes == Counter({"budget_exhausted": WORKERS})  # one refusal per loop, all money
    assert held == 0 and spent <= usd(CAP)
    assert s.fake.calls == served  # no refused call reached the upstream
    assert spent == served * priced(s, FULL)  # each served call settled once, at usage
    # The last refusal saw no other hold open, so it was refused on spent alone: less than one
    # call's hold is left. The loop ran the budget down, it did not stop early.
    assert usd(CAP) - spent < s.observer.held[0].hold.nano
    assert served > 2 * WORKERS


# T-SESS-02
@async_test
async def test_t_sess_02_m06_parallel_loops_on_one_key_stop_at_the_cap_on_sqlite(
    tmp_path: Path,
) -> None:
    async with sqlite_books(tmp_path) as (s, books):
        await check_runaway_stops_at_the_cap(s, books)


@pytest.mark.postgres
def test_t_sess_02_m06_parallel_loops_on_one_key_stop_at_the_cap_on_postgres(
    tmp_path: Path, postgres_url: str
) -> None:
    async def case() -> None:
        async with postgres_books(tmp_path) as (s, books):
            await check_runaway_stops_at_the_cap(s, books)

    assert postgres_url  # the fixture skips without a server; workdir_location reads the env
    asyncio.run(case())


# T-SESS-03
@async_test
async def test_t_sess_03_m13_a_kill_switch_mid_loop_refuses_the_next_call_and_stops_the_stream(
    tmp_path: Path,
) -> None:
    async with gated(tmp_path) as (s, up):
        long = await open_call(s)  # one step of the loop is a stream still running
        up.streams[0].put(chat_chunk("a"))
        assert await long.chunk() == chat_chunk("a")
        for n in range(1, 4):  # the loop goes on beside it: three short streams end normally
            short = await open_call(s)
            up.streams[n].put(chat_usage(SEEN))
            up.streams[n].end()
            await short.read_all()
            await short.finish()
            assert short.status == 200
        await engage(s, KillTarget.SCOPE, s.scope.id)
        refused = await open_call(s)  # returns at the headers: a regression fails, not hangs
        assert refused.status == 503
        assert json.loads(await refused.read_all())["error"]["code"] == "kill_switch"
        await refused.finish()
        assert len(up.requests) == 4  # the refused call never reached the upstream
        up.streams[0].put(chat_chunk("b"))
        assert await long.chunk() == chat_chunk("b")  # until the heartbeat, the stream runs
        await beat(s)  # one heartbeat
        text = (await long.read_all()).decode()
        assert "kill switch" in text and '"error"' in text
        await long.finish()
        assert up.streams[0].closed
        await s.pipeline.aclose()
        hold = s.observer.held[0].hold.nano  # the stopped stream saw no usage: full hold
        assert await s.totals() == (3 * priced(s) + hold, 0)
