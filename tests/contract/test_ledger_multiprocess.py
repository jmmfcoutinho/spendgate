"""Real processes contending for one ledger store: no overshoot, no lost update (T-LED-03)."""

import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import pytest

from tests.contract.rig import E, Rig, hold_worker
from tests.contract.test_ledger import assert_i1, figures

PROCESSES = 8
HOLDS_EACH = 50


@pytest.mark.slow
def test_t_led_03_eight_processes_fifty_holds_each_never_overshoot_the_cap(
    adapter: str, rig: Rig
) -> None:
    cap_units = 120  # fewer than the 400 attempts, so refusals must happen
    w = rig.world(str(cap_units))
    rig.close_connection()  # the parent stays out of the way while the children run
    context = multiprocessing.get_context("spawn")  # no inherited connections or locks
    with ProcessPoolExecutor(PROCESSES, mp_context=context) as pool:
        jobs = [
            pool.submit(
                hold_worker,
                adapter,
                rig.location,
                w.scope.id,
                w.key.id,
                f"p{n}",
                HOLDS_EACH,
                E("1"),
            )
            for n in range(PROCESSES)
        ]
        admitted = sum(job.result(timeout=300) for job in jobs)
    rig.restart()
    t = rig.totals(w)
    assert admitted == cap_units  # exactly the room there was: nothing lost, nothing extra
    assert figures(t) == (0, E(str(cap_units)), 0, 0)
    assert_i1(t)


@pytest.mark.slow
def test_t_led_03_mixed_holds_and_settles_across_processes_keep_the_books_exact(
    adapter: str, rig: Rig
) -> None:
    w = rig.world("120")
    rig.close_connection()
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(PROCESSES, mp_context=context) as pool:
        jobs = [
            pool.submit(
                hold_worker,
                adapter,
                rig.location,
                w.scope.id,
                w.key.id,
                f"m{n}",
                HOLDS_EACH,
                E("1"),
                E("0.5"),
            )
            for n in range(PROCESSES)
        ]
        admitted = sum(job.result(timeout=300) for job in jobs)
    rig.restart()
    t = rig.totals(w)
    assert admitted >= 120  # settling at half frees room, so more than 120 get through
    assert figures(t) == (admitted * E("0.5"), 0, 0, 0)
    assert t.calls == admitted
    assert_i1(t)
