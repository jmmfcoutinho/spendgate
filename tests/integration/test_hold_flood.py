"""A flood of calls from one key opens at most its concurrency in holds (C-24, T-HOLD-03).

Every admitted call holds its worst case until it ends, so open holds are what a key can lock of
its budget (R-05). The key's `concurrency` bounds them: the rest are refused before any hold and
before anything is sent.
"""

import asyncio
from pathlib import Path

from tests.fakes import Script
from tests.integration.harness import async_test, open_stack, wait_until

CONCURRENCY = 2
FLOOD = 12


@async_test
async def test_t_hold_03_a_flood_of_calls_from_one_key_is_capped_by_its_concurrency(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path, max_concurrent=CONCURRENCY) as s:
        s.fake.default = Script(hang=True)  # admitted calls stay open, holding their worst case
        calls = [asyncio.ensure_future(s.chat()) for _ in range(FLOOD)]
        await wait_until(lambda: sum(c.done() for c in calls) == FLOOD - CONCURRENCY)
        await wait_until(lambda: s.fake.calls == CONCURRENCY)
        refused = [c.result() for c in calls if c.done()]
        assert {(r.status_code, r.json()["error"]["code"]) for r in refused} == {
            (429, "rate_limited")
        }
        assert len(s.observer.held) == CONCURRENCY
        _, held = await s.totals()
        assert held == sum(e.hold.nano for e in s.observer.held)  # only the admitted calls hold
        assert s.fake.calls == CONCURRENCY  # nothing refused was sent
        for c in calls:
            c.cancel()
        await asyncio.gather(*calls, return_exceptions=True)
        await s.pipeline.aclose()
        spent, held = await s.totals()
        assert held == 0 and spent == sum(e.hold.nano for e in s.observer.held)  # sent: charged
