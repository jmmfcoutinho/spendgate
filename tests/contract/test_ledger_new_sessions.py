"""M-20, T-SESS-10: holds racing to open new sessions on one key never pass the hourly limit.

The limit is a count of the key's sessions made in the last hour, taken inside the hold. On
Postgres (READ COMMITTED) concurrent holds would each count below the limit and each insert, so
new-session admission takes the key row lock before it counts. SQLite serialises writers. Real
processes start each round together at a barrier, so their holds overlap.
"""

import asyncio
import multiprocessing
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from threading import Barrier

import pytest

from spendgate.core.errors import ErrorCode, LimitName
from spendgate.core.policy import KeyPolicy
from spendgate.core.sessions import SessionDefaults
from spendgate.ledger.port import Candidate, Held, HoldMeta, HoldRequest, Refused
from spendgate.ledger.postgres import PostgresLedger
from tests.contract.rig import LEASE_MS, T0, Rig, close_ledger, open_ledger

pytestmark = pytest.mark.slow

PROCESSES = 8
ROUNDS = 6  # one fresh key per round: each round is a separate race at the barrier
ATTEMPTS = 3  # new sessions each process tries per round, all at once with the others
LIMIT = 3
DEFAULTS = SessionDefaults(max_new_sessions_per_hour=LIMIT)
BARRIER_SECONDS = 120


def worker(  # noqa: PLR0913, PLR0917 - a spawned process gets everything as plain arguments
    kind: str,
    location: str,
    keys: list[KeyPolicy],
    barrier: Barrier,
    prefix: str,
    plain_holds: bool,
) -> tuple[list[int], dict[str, int]]:
    """Per round: wait at the barrier, then try `ATTEMPTS` new sessions on that round's key.

    With `plain_holds`, a hold without a session on the same key follows each attempt. Returns
    the sessions admitted per round and the retries the Postgres driver saw, by SQLSTATE.
    """
    ledger = open_ledger(kind, location)
    loop = asyncio.new_event_loop()
    meta = HoldMeta("/v1/chat", "gpt", "sha", (Candidate("p1", "d", "price"),))
    admitted: list[int] = []
    try:
        for round_no, key in enumerate(keys):
            barrier.wait(BARRIER_SECONDS)
            opened = 0
            for n in range(ATTEMPTS):
                rid = f"{prefix}-r{round_no}-{n}"
                session = f"{prefix}-r{round_no}-s{n}"  # a new session every time
                request = HoldRequest(
                    rid, prefix, T0, LEASE_MS, 1_000, key, "gpt", ("p1",), meta, session
                )
                held = loop.run_until_complete(ledger.hold(request))
                if isinstance(held, Held):
                    opened += 1
                else:
                    assert isinstance(held, Refused), held
                    assert held.refusal.code is ErrorCode.SESSION_LIMIT_REACHED
                    assert held.refusal.details["limit"] == LimitName.NEW_SESSIONS.value
                if plain_holds:
                    plain = HoldRequest(
                        f"{rid}-plain", prefix, T0, LEASE_MS, 1_000, key, "gpt", ("p1",), meta
                    )
                    assert isinstance(loop.run_until_complete(ledger.hold(plain)), Held)
            admitted.append(opened)
        retries = dict(ledger.stats.by_sqlstate) if isinstance(ledger, PostgresLedger) else {}
        return admitted, retries
    finally:
        close_ledger(ledger)  # before the loop closes: a Postgres pool lives on that loop
        loop.close()


def race(adapter: str, rig: Rig, *, plain_holds: bool) -> tuple[list[int], Counter[str]]:
    """Run every round; returns the sessions admitted per key and the retries by SQLSTATE."""
    w = rig.world("1000", session_defaults=DEFAULTS, key_id="key-0")
    keys = [w.key] + [rig.add_key(w, f"key-{n}", DEFAULTS) for n in range(1, ROUNDS)]
    rig.close_connection()  # the parent stays out of the way while the children run
    context = multiprocessing.get_context("spawn")  # no inherited connections or locks
    with context.Manager() as manager, ProcessPoolExecutor(PROCESSES, mp_context=context) as pool:
        barrier = manager.Barrier(PROCESSES)
        jobs = [
            pool.submit(worker, adapter, rig.location, keys, barrier, f"w{n}", plain_holds)
            for n in range(PROCESSES)
        ]
        per_key = [0] * ROUNDS
        retries: Counter[str] = Counter()
        for job in jobs:
            admitted, seen = job.result(timeout=600)
            per_key = [a + b for a, b in zip(per_key, admitted, strict=True)]
            retries.update(seen)
    rig.restart()
    return per_key, retries


def test_m20_concurrent_new_sessions_on_one_key_never_exceed_max_new_sessions_per_hour(
    adapter: str, rig: Rig
) -> None:
    per_key, _ = race(adapter, rig, plain_holds=False)
    # Each key had PROCESSES x ATTEMPTS tries at once: exactly the limit got a session.
    assert per_key == [LIMIT] * ROUNDS


def test_t_sess_10_new_sessions_beside_plain_holds_on_one_key_never_deadlock(
    adapter: str, rig: Rig
) -> None:
    # Plain holds check the reservation's key foreign key while they hold budget locks; the
    # new-session key lock must let that check through (FOR NO KEY UPDATE), or the two deadlock.
    per_key, retries = race(adapter, rig, plain_holds=True)
    assert per_key == [LIMIT] * ROUNDS
    assert retries["40P01"] == 0, dict(retries)
