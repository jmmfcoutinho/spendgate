"""Many processes hold across overlapping budget chains: no deadlock, no overshoot."""

import asyncio
import multiprocessing
import random
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from spendgate.core.budgets import Period, ScopeKind
from spendgate.core.outcomes import Outcome
from spendgate.core.policy import KeyPolicy
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

PROCESSES = 8
TOTAL_HOLDS = 10_000
PROJECTS = 3
USERS_PER_PROJECT = 4
# Caps small enough that many holds are refused, so the refusal path is hammered too.
ORG_CAP, PROJECT_CAP, USER_CAP = 6_000_000, 2_400_000, 900_000
WORST_RANGE = (1_000, 9_000)


def worker(
    location: str, leaves: list[tuple[str, str]], prefix: str, count: int, seed: int
) -> tuple[dict[str, int], dict[str, int], int]:
    """`count` holds on random leaves; each admitted hold is settled or released at random.

    Returns the money booked per leaf, the retries seen by SQLSTATE, and the refusals.
    """
    rng = random.Random(seed)  # noqa: S311 - a workload, not a secret
    ledger = open_ledger("postgres", location)
    assert isinstance(ledger, PostgresLedger)
    loop = asyncio.new_event_loop()
    booked: dict[str, int] = {}
    refused = 0
    meta = HoldMeta("/v1/chat", "gpt", "sha", (Candidate("p1", "d", "price"),))
    try:
        for n in range(count):
            scope_id, key_id = rng.choice(leaves)
            worst = rng.randint(*WORST_RANGE)
            request = HoldRequest(
                f"{prefix}-{n}",
                prefix,
                T0,
                LEASE_MS,
                worst,
                KeyPolicy(key_id, scope_id),
                "gpt",
                ("p1",),
                meta,
            )
            held = loop.run_until_complete(ledger.hold(request))  # a deadlock would raise here
            if not isinstance(held, Held):
                refused += 1
            elif rng.random() < 0.2:
                made = (attempt(1, Outcome.THROTTLED, 0, T0, True),)
                loop.run_until_complete(ledger.release(request.request_id, made, T0))
            else:
                made = (attempt(1, Outcome.OK, worst, T0, usage_cost_nano=worst),)
                loop.run_until_complete(ledger.settle(request.request_id, worst, made, T0))
                booked[scope_id] = booked.get(scope_id, 0) + worst
        return booked, dict(ledger.stats.by_sqlstate), refused
    finally:
        close_ledger(ledger)
        loop.close()


def build_chains(rig: Rig) -> tuple[list[tuple[str, str]], dict[str, list[str]]]:
    """org -> projects -> users, a budget on every scope. Returns (leaf, key) pairs and the tree."""
    org = rig.run(rig.ledger.create_scope(ScopeKind.ORG, "acme", None, now=T0))
    rig.run(rig.ledger.create_budget(org.id, Period.NONE, ORG_CAP, now=T0))
    leaves: list[tuple[str, str]] = []
    tree: dict[str, list[str]] = {}
    for p in range(PROJECTS):
        project = rig.run(rig.ledger.create_scope(ScopeKind.PROJECT, f"p{p}", org.id, now=T0))
        rig.run(rig.ledger.create_budget(project.id, Period.NONE, PROJECT_CAP, now=T0))
        tree[project.id] = []
        for u in range(USERS_PER_PROJECT):
            user = rig.run(rig.ledger.create_scope(ScopeKind.USER, f"u{p}{u}", project.id, now=T0))
            rig.run(rig.ledger.create_budget(user.id, Period.NONE, USER_CAP, now=T0))
            rig.run(rig.ledger.add_key_record(f"key-{p}-{u}", user.id, now=T0))
            leaves.append((user.id, f"key-{p}-{u}"))
            tree[project.id].append(user.id)
    tree["org"] = [org.id]
    return leaves, tree


def test_ten_thousand_concurrent_holds_across_eight_processes(
    postgres_url: str, tmp_path: Path, new_id: Callable[[], str]
) -> None:
    rig = Rig("postgres", workdir_location("postgres", tmp_path), new_id)
    try:
        leaves, tree = build_chains(rig)
        rig.close_connection()
        context = multiprocessing.get_context("spawn")
        started = time.monotonic()
        with ProcessPoolExecutor(PROCESSES, mp_context=context) as pool:
            jobs = [
                pool.submit(
                    worker, rig.location, leaves, f"w{n}", TOTAL_HOLDS // PROCESSES, 1000 + n
                )
                for n in range(PROCESSES)
            ]
            results = [job.result(timeout=900) for job in jobs]  # raises on any failed hold
        seconds = time.monotonic() - started
        rig.restart()

        booked: Counter[str] = Counter()
        retries: Counter[str] = Counter()
        for money, by_state, _ in results:
            booked.update(money)
            retries.update(by_state)
        refused = sum(r[2] for r in results)
        print(
            f"\n{TOTAL_HOLDS} holds in {seconds:.1f}s, refused {refused}, "
            f"admitted-and-settled {sum(booked.values())} nano, retries {dict(retries)}"
        )
        assert refused > 0  # the caps really bound
        _check_books(rig, tree, booked)
    finally:
        rig.close()


def _check_books(rig: Rig, tree: dict[str, list[str]], booked: Counter[str]) -> None:
    """Spent equals what the workers booked; nothing is held; no budget is over its cap."""
    org_total = 0
    for project_id, users in tree.items():
        if project_id == "org":
            continue
        project_booked = 0
        for user_id in users:
            (user,) = rig.run(rig.ledger.totals(user_id, T0))
            assert user.spent_nano == booked[user_id]
            assert user.held_nano == 0
            assert user.spent_nano <= USER_CAP
            project_booked += booked[user_id]
        (project,) = rig.run(rig.ledger.totals(project_id, T0))
        assert project.spent_nano == project_booked <= PROJECT_CAP
        assert project.held_nano == 0
        org_total += project_booked
    (org,) = rig.run(rig.ledger.totals(tree["org"][0], T0))
    assert org.spent_nano == org_total <= ORG_CAP
    assert org.held_nano == 0
