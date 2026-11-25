"""Holds and releases on one session at once queue on its row instead of deadlocking (#66).

Hold locks the session scope row, then the budgets; release must take them in the same order
(docs/ledger-state-machine.md, lock order). Crossed orders show up as 40P01 retries.
"""

import asyncio
import multiprocessing
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from spendgate.core.outcomes import Outcome
from spendgate.core.policy import KeyPolicy
from spendgate.core.sessions import SessionDefaults
from spendgate.ledger.port import Candidate, Held, HoldMeta, HoldRequest
from spendgate.ledger.postgres import PostgresLedger
from tests.contract.rig import (
    LEASE_MS,
    T0,
    Rig,
    attempt,
    close_ledger,
    open_ledger,
    workdir_location,
)

pytestmark = [pytest.mark.postgres, pytest.mark.slow]

PROCESSES = 6
CALLS_PER_PROCESS = 150
SESSION = "agent-run-1"
DEFAULTS = SessionDefaults(max_steps=1_000_000)


def worker(location: str, key: KeyPolicy, prefix: str, count: int) -> dict[str, int]:
    """Hold and release `count` calls on the shared session; returns retries by SQLSTATE."""
    ledger = open_ledger("postgres", location)
    assert isinstance(ledger, PostgresLedger)
    loop = asyncio.new_event_loop()
    meta = HoldMeta("/v1/chat", "gpt", "sha", (Candidate("p1", "d", "price"),))
    free = (attempt(1, Outcome.THROTTLED, 0, T0, True),)
    try:
        for n in range(count):
            rid = f"{prefix}-{n}"
            request = HoldRequest(
                rid, prefix, T0, LEASE_MS, 1_000, key, "gpt", ("p1",), meta, SESSION
            )
            assert isinstance(loop.run_until_complete(ledger.hold(request)), Held)
            loop.run_until_complete(ledger.release(rid, free, T0))
        return dict(ledger.stats.by_sqlstate)
    finally:
        close_ledger(ledger)
        loop.close()


def test_holds_and_releases_on_one_session_never_deadlock(
    postgres_url: str, tmp_path: Path, new_id: Callable[[], str]
) -> None:
    rig = Rig("postgres", workdir_location("postgres", tmp_path), new_id)
    try:
        w = rig.world("1000", session_defaults=DEFAULTS)
        rig.hold_ok(w, "warm-up", "0.000001", session=SESSION)  # the session row exists
        rig.run(rig.ledger.release("warm-up", (attempt(1, Outcome.THROTTLED, 0, T0, True),), T0))
        rig.close_connection()
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(PROCESSES, mp_context=context) as pool:
            jobs = [
                pool.submit(worker, rig.location, w.key, f"w{n}", CALLS_PER_PROCESS)
                for n in range(PROCESSES)
            ]
            retries: Counter[str] = Counter()
            for job in jobs:
                retries.update(job.result(timeout=600))
        rig.restart()
        assert retries["40P01"] == 0, dict(retries)
        (totals,) = rig.run(rig.ledger.totals(w.scope.id, T0))
        assert totals.held_nano == 0 and totals.spent_nano == 0
    finally:
        rig.close()
