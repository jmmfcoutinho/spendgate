"""T-CLK-04: holds, settles, releases and expiries racing across a UTC period boundary.

A hold is booked in the period its clock reading falls in, and settle, release, expiry and late
settle book into that period whatever the clock reads then (ledger-state-machine.md, "Periods").
Real processes start each phase together at a barrier with clocks a few milliseconds either side
of midnight, as instances whose clocks differ slightly would be: the first holds of the new period
race to create its row while holds of the old one settle, expire or are released into the old row.

Afterwards every `budget_periods` row is compared with a model built from the scripts: one row per
budget and period start, `held` back to 0, each call's charge booked once in its own period (and
in the month budget's row as well as the day budget's), the `calls` rows adding up to the same
spend, the new period's cap exact, and each alert reported once per transition.
"""

import asyncio
import multiprocessing
import random
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from spendgate.core.budgets import AlertKind, Budget, Period, PeriodState, invariant_holds
from spendgate.core.budgets import period_start as start_of
from spendgate.core.outcomes import Outcome
from spendgate.core.policy import KeyPolicy
from spendgate.ledger.admin_port import AdminLedger
from spendgate.ledger.port import Candidate, Held, HoldMeta, HoldRequest
from tests.contract.rig import LEASE_MS, E, Rig, attempt, close_ledger, open_ledger
from tests.contract.test_migration_policy import execute

pytestmark = pytest.mark.slow

PROCESSES = 8
BARRIER_SECONDS = 120
DAY_CAP = 40  # whole units; every hold is WORST (one unit), so a period holds DAY_CAP of them
WORST = E("1")
MONTH_CAP = E("100000")  # an org budget that never limits: it only has to book every call once
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MID_MONTH = (datetime(2023, 11, 15, tzinfo=UTC) - _EPOCH) // timedelta(milliseconds=1)
MONTH_START = (datetime(2023, 12, 1, tzinfo=UTC) - _EPOCH) // timedelta(milliseconds=1)
HOUR_MS = 3_600_000
SWEEP_AFTER = 500  # ms after the boundary: past every short lease, inside every normal one

type Step = tuple[str, str, int, int, int]  # op, request id, clock, amount, lease ms
type Alert = tuple[AlertKind, str, int]  # kind, budget id, period start
type Result = tuple[str, str, str, tuple[Alert, ...]]  # op, request id, verdict, alerts


@dataclass(frozen=True, slots=True)
class Call:
    """One planned hold and what its owner does with it once the clock has rolled.

    `expire`: the owner vanishes and the sweeper charges the hold. `late`: the same, and the owner
    then settles above the hold, so whichever of owner and sweeper goes first, `charge` is booked.
    """

    rid: str
    clock: int  # the hold's clock reading, which decides its period
    fate: str  # settle, release, expire or late
    charge: int = 0
    worst: int = WORST

    def __post_init__(self) -> None:
        assert self.fate != "late" or self.charge > self.worst  # else the order would matter

    @property
    def booked(self) -> int:
        return {"release": 0, "expire": self.worst}.get(self.fate, self.charge)

    def hold(self) -> Step:
        lease = 1 if self.fate in {"expire", "late"} else LEASE_MS
        return ("hold", self.rid, self.clock, self.worst, lease)

    def close(self, clock: int) -> list[Step]:
        if self.fate == "expire":
            return []
        if self.fate == "release":
            return [("release", self.rid, clock, 0, 0)]
        return [("settle", self.rid, clock, self.charge, 0)]


def sweep(boundary: int) -> Step:
    return ("sweep", "", boundary + SWEEP_AFTER, 0, 0)


def hold_request(step: Step, key: KeyPolicy) -> HoldRequest:
    _, rid, clock, amount, lease = step
    meta = HoldMeta("/v1/chat", "gpt", "sha", (Candidate("p1", "d", "price"),))
    return HoldRequest(rid, "i", clock, lease, amount, key, "gpt", ("p1",), meta)


# --- the processes ---------------------------------------------------------------------------


async def run_step(ledger: AdminLedger, key: KeyPolicy, held: set[str], step: Step) -> Result:
    """One step; a close of a hold that was refused is skipped. Returns what happened."""
    op, rid, clock, amount, _ = step
    if op == "sweep":
        await ledger.sweep_expired(clock)
        return (op, rid, "swept", ())
    if op == "hold":
        result = await ledger.hold(hold_request(step, key))
        if isinstance(result, Held):
            held.add(rid)
            return (
                op,
                rid,
                "held",
                tuple((a.kind, a.budget_id, a.period_start) for a in result.alerts),
            )
        refusal = result.refusal
        return (op, rid, str(refusal.details.get("reason", refusal.code.value)), ())
    if rid not in held:
        return (op, rid, "skipped", ())
    if op == "release":
        done = await ledger.release(rid, (attempt(1, Outcome.THROTTLED, 0, clock, True),), clock)
    else:
        made = (attempt(1, Outcome.OK, amount, clock, usage_cost_nano=amount),)
        done = await ledger.settle(rid, amount, made, clock)
    assert done.applied, (step, done)  # exactly one money transition per close: M-05
    alerts = tuple((a.kind, a.budget_id, a.period_start) for a in done.alerts)
    return (op, rid, "late" if done.late else "closed", alerts)


def worker(
    kind: str, location: str, key: KeyPolicy, barrier: Barrier, phases: list[list[Step]]
) -> list[Result]:
    """Run each phase's steps once every process has reached the barrier."""
    ledger = open_ledger(kind, location)
    loop = asyncio.new_event_loop()
    held: set[str] = set()
    out: list[Result] = []
    try:
        for steps in phases:
            barrier.wait(BARRIER_SECONDS)
            out += [loop.run_until_complete(run_step(ledger, key, held, s)) for s in steps]
        return out
    finally:
        close_ledger(ledger)  # before the loop closes: a Postgres pool lives on that loop
        loop.close()


def race(adapter: str, rig: Rig, key: KeyPolicy, scripts: list[list[list[Step]]]) -> list[Result]:
    rig.close_connection()  # the parent stays out of the way while the children run
    context = multiprocessing.get_context("spawn")  # no inherited connections or locks
    with context.Manager() as manager, ProcessPoolExecutor(PROCESSES, mp_context=context) as pool:
        barrier = manager.Barrier(PROCESSES)
        jobs = [pool.submit(worker, adapter, rig.location, key, barrier, p) for p in scripts]
        results = [r for job in jobs for r in job.result(timeout=600)]
    rig.restart()
    return results


# --- the books -------------------------------------------------------------------------------


def world(rig: Rig, boundary: int, *, strict: bool = False) -> tuple[KeyPolicy, Budget, Budget]:
    """A day budget on the user scope, a month budget on its org; returns key, day, month."""
    setup = boundary - HOUR_MS
    w = rig.world(str(DAY_CAP), period=Period.DAY, strict_overrun=strict, now=setup)
    month = rig.run(
        rig.ledger.create_budget(
            w.parent.id, Period.MONTH, MONTH_CAP, now=setup, strict_overrun=False
        )
    )
    return w.key, w.budget, month


def expected_rows(
    budgets: tuple[Budget, ...], calls: list[Call], held: set[str]
) -> dict[tuple[str, int], list[int]]:
    """(budget, period start) -> [spent, held, overrun, excess, calls_count] once all is closed.

    A refused hold still makes its period's row (its excess is normalised there, A1).
    """
    rows: dict[tuple[str, int], list[int]] = {}
    for budget in budgets:
        for call in calls:
            row = rows.setdefault((budget.id, start_of(budget.period, call.clock)), [0] * 5)
            if call.rid in held:
                row[0] += call.booked
                row[2] += max(0, call.booked - call.worst)
                row[4] += call.fate != "release"
    return rows


def check_books(rig: Rig, budgets: tuple[Budget, ...], calls: list[Call], held: set[str]) -> None:
    """The stored periods equal the model, satisfy I1, and agree with `calls` and reservations."""
    kind, where = rig.kind, rig.location
    stored = execute(
        kind,
        where,
        "SELECT budget_id, period_start, spent_nano, held_nano, overrun_nano, excess_nano,"
        " calls_count FROM budget_periods",
    )
    found = {(str(r[0]), int(str(r[1]))): [int(str(v)) for v in r[2:]] for r in stored}
    assert len(found) == len(stored)  # one row per (budget, period start)
    assert found == expected_rows(budgets, calls, held)
    caps = {b.id: b.cap_nano for b in budgets}
    for (budget_id, _), (spent, in_hold, overrun, excess, _) in found.items():
        assert invariant_holds(PeriodState(spent, in_hold, overrun, excess), caps[budget_id])
    booked = execute(
        kind,
        where,
        "SELECT it.budget_id, it.period_start, SUM(c.charged_nano) FROM calls c"
        " JOIN reservation_items it ON it.reservation_id = c.request_id"
        " GROUP BY it.budget_id, it.period_start",
    )
    by_calls = {(str(r[0]), int(str(r[1]))): int(str(r[2])) for r in booked}
    assert {k: v for k, v in by_calls.items() if v} == {k: v[0] for k, v in found.items() if v[0]}
    states = execute(
        kind, where, "SELECT state, SUM(charged_nano) FROM reservations GROUP BY state"
    )
    assert "pending" not in {str(r[0]) for r in states}
    assert sum(int(str(r[1] or 0)) for r in states) == sum(c.booked for c in calls if c.rid in held)


def admitted(results: list[Result]) -> set[str]:
    return {rid for op, rid, verdict, _ in results if op == "hold" and verdict == "held"}


def alert_counts(results: list[Result]) -> Counter[Alert]:
    return Counter(alert for *_, alerts in results for alert in alerts)


def finish(rig: Rig, boundary: int, results: list[Result]) -> set[str]:
    """A last sweep for any expiring hold no sweep saw yet; returns the admitted request ids."""
    rig.sweep(boundary + SWEEP_AFTER)
    return admitted(results)


# --- mixed fates, with clocks either side of midnight ----------------------------------------

HOLDS = 8  # per process and phase: 64 tries on DAY_CAP units of room


def mixed_call(rng: random.Random, rid: str, clock: int) -> Call:
    fate = rng.choice(["settle", "settle", "release", "expire", "late"])
    charge = {
        "settle": E(rng.choice(["0", "0.25", "0.50", "1", "1.50"])),
        "late": E("1.75"),
    }.get(fate, 0)
    return Call(rid, clock, fate, charge)


def mixed_scripts(boundary: int) -> tuple[list[list[list[Step]]], list[Call]]:
    """Phase 1 fills the old day. Phase 2 closes those holds after midnight while new holds are
    taken, by a quarter of the processes on a clock that still reads the old day. Phase 3 closes
    the rest. Holds of the new day stay open until phase 3, so its cap must admit exactly
    `DAY_CAP` of them; `late` holds are settled in phase 3, mostly after a sweep took them."""
    scripts: list[list[list[Step]]] = []
    calls: list[Call] = []
    for n in range(PROCESSES):
        rng = random.Random(n)  # noqa: S311 - a seeded workload, not a secret
        behind = n % 4 == 0
        old = [mixed_call(rng, f"w{n}-a{k}", boundary - 1 - n * HOLDS - k) for k in range(HOLDS)]
        new = [
            mixed_call(rng, f"w{n}-b{k}", boundary - 1 - k if behind else boundary + k)
            for k in range(HOLDS)
        ]
        second: list[Step] = []
        for k in range(HOLDS):
            if old[k].fate != "late":
                second += old[k].close(boundary + k)
            second += [new[k].hold(), *([sweep(boundary)] if k % 2 else [])]
        late = [s for c in old if c.fate == "late" for s in c.close(boundary + 200)]
        third = [s for k, c in enumerate(new) for s in c.close(boundary + 100 + k)]
        scripts.append([[c.hold() for c in old], second, [*third, *late, sweep(boundary)]])
        calls += old + new
    return scripts, calls


@pytest.mark.parametrize("boundary", [MID_MONTH, MONTH_START], ids=["midnight", "month_start"])
def test_t_clk_04_holds_racing_across_midnight_book_each_call_once_in_its_own_period(
    adapter: str, rig: Rig, boundary: int
) -> None:
    key, day, month = world(rig, boundary)
    scripts, calls = mixed_scripts(boundary)
    results = race(adapter, rig, key, scripts)
    held = finish(rig, boundary, results)
    old_day, new_day = start_of(Period.DAY, boundary - 1), start_of(Period.DAY, boundary)
    phase1 = {c.rid for c in calls if "-a" in c.rid}
    new_clock = {c.rid for c in calls if start_of(Period.DAY, c.clock) == new_day}
    assert len(held & phase1) == DAY_CAP  # the old day filled exactly
    assert len(held & new_clock) == DAY_CAP  # the new day's own cap, whatever the old one did
    refused = {v for op, _, v, _ in results if op == "hold" and v != "held"}
    assert refused == {"budget_period_exhausted"}
    check_books(rig, (day, month), calls, held)
    alerts = alert_counts(results)
    soft = {a: n for a, n in alerts.items() if a[0] is AlertKind.SOFT_THRESHOLD}
    assert soft == {
        (AlertKind.SOFT_THRESHOLD, day.id, old_day): 1,
        (AlertKind.SOFT_THRESHOLD, day.id, new_day): 1,
    }
    assert {a[0] for a in alerts} <= {AlertKind.SOFT_THRESHOLD, AlertKind.CAP_BREACHED}
    for start in (old_day, new_day):
        mine = [c for c in calls if c.rid in held and start_of(Period.DAY, c.clock) == start]
        spent = sum(c.booked for c in mine)
        crossings = alerts[(AlertKind.CAP_BREACHED, day.id, start)]
        overruns = sum(c.booked > c.worst for c in mine)
        assert crossings <= overruns and (crossings >= 1 or spent <= E(str(DAY_CAP)))
    assert not any(a[1] == month.id and a[0] is AlertKind.CAP_BREACHED for a in alerts)


# --- a nearly exhausted new day while the old day overruns -----------------------------------

OLD_EACH = DAY_CAP // PROCESSES  # phase 1: exactly the old day's room, all admitted
NEW_EACH = 8
SEED = 30  # units spent in the new day before the race: 75 %, just under the soft threshold


def roll_scripts(boundary: int) -> tuple[list[list[list[Step]]], list[Call]]:
    """Old-day holds only ever add to `S + H` after midnight (settled at or above the hold, or
    expired), so the first overrun crosses the cap and no later one can; each process's first
    hold overruns. Meanwhile 64 new-day holds race for the `DAY_CAP - SEED` units left."""
    fates = [("settle", E("1.50")), ("settle", WORST), ("expire", 0), ("late", E("1.75"))]
    scripts: list[list[list[Step]]] = []
    calls: list[Call] = []
    for n in range(PROCESSES):
        old = [
            Call(f"w{n}-a{k}", boundary - 1 - n * OLD_EACH - k, *fates[k % len(fates)])
            for k in range(OLD_EACH)
        ]
        new = [Call(f"w{n}-b{k}", boundary + k, "settle", WORST) for k in range(NEW_EACH)]
        second: list[Step] = []
        for k in range(NEW_EACH):
            second += [*(old[k].close(boundary + k) if k < OLD_EACH else []), new[k].hold()]
            second += [sweep(boundary)] if k % 2 else []
        third = [s for k, c in enumerate(new) for s in c.close(boundary + 100 + k)]
        scripts.append([[c.hold() for c in old], second, third])
        calls += old + new
    return scripts, calls


@pytest.mark.parametrize("strict", [False, True], ids=["lenient", "strict"])
def test_t_clk_04_settles_into_the_old_day_never_give_room_in_a_nearly_exhausted_new_one(
    adapter: str, rig: Rig, strict: bool
) -> None:
    boundary = MONTH_START  # both budgets roll at once
    key, day, month = world(rig, boundary, strict=strict)
    seed = Call("seed", boundary + 1, "settle", E(str(SEED)), E(str(SEED)))
    seeded = rig.run(rig.ledger.hold(hold_request(seed.hold(), key)))
    assert isinstance(seeded, Held) and seeded.alerts == ()
    assert rig.settle("seed", str(SEED), boundary + 2).applied
    scripts, calls = roll_scripts(boundary)
    results = race(adapter, rig, key, scripts)
    held = finish(rig, boundary, results) | {"seed"}
    old_day, new_day = start_of(Period.DAY, boundary - 1), start_of(Period.DAY, boundary)
    phase1 = {c.rid for c in calls if "-a" in c.rid}
    assert phase1 <= held
    new_held = len(held - phase1 - {"seed"})
    check_books(rig, (day, month), [seed, *calls], held)
    alerts = alert_counts(results)
    soft_new = alerts[(AlertKind.SOFT_THRESHOLD, day.id, new_day)]
    assert alerts[(AlertKind.SOFT_THRESHOLD, day.id, old_day)] == 1
    assert alerts[(AlertKind.CAP_BREACHED, day.id, old_day)] == 1
    assert alerts[(AlertKind.CAP_BREACHED, day.id, new_day)] == 0
    assert not any(a[1] == month.id for a in alerts)
    refused = {v for op, _, v, _ in results if op == "hold" and v != "held"}
    if strict:  # the old day's breach blocks the budget, across the roll (M-07)
        assert alerts[(AlertKind.BLOCKED, day.id, old_day)] == 1 and len(alerts) == 3 + soft_new
        assert new_held <= DAY_CAP - SEED and soft_new <= 1
        assert (
            refused <= {"budget_period_exhausted", "overrun_review"} and "overrun_review" in refused
        )
    else:
        assert new_held == DAY_CAP - SEED and soft_new == 1 and len(alerts) == 3
        assert refused == {"budget_period_exhausted"}
