"""Limit algorithms: exact admission, refunds, all-or-nothing, TPM reconcile."""

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from spendgate.core.limits import (
    Admit,
    Deny,
    InMemoryLimitStore,
    LimitKey,
    LimitStore,
    WindowState,
    admit_all,
    refund,
    used,
)

WINDOW = 60_000
RPM = LimitKey("rpm", 5, WINDOW)
TPM = LimitKey("tpm", 100, WINDOW)
CONC = LimitKey("conc", 2, None)


def _acquire(store: LimitStore, keys: list[LimitKey], amounts: list[int], now: int) -> Admit | Deny:
    return asyncio.run(store.try_acquire(keys, amounts, now))


ops = st.lists(
    st.tuples(st.integers(0, 30_000), st.integers(1, 60)),
    max_size=40,
)


@settings(max_examples=1000, deadline=None)
@given(ops=ops, limit=st.integers(1, 100))
def test_admitted_never_exceeds_limit_in_any_window(ops: list[tuple[int, int]], limit: int) -> None:
    key = LimitKey("tpm", limit, WINDOW)
    states: dict[str, WindowState] = {}
    now = 0
    admitted: list[tuple[int, int]] = []
    for ticket, (gap, amount) in enumerate(ops):  # explicit fake time: monotone, never real
        now += gap
        if admit_all(states, [key], [amount], now, ticket) is None:
            admitted.append((now, amount))
    for end, _ in admitted:
        in_window = sum(a for t, a in admitted if end - WINDOW < t <= end)
        assert in_window <= limit


@settings(max_examples=1000, deadline=None)
@given(steps=st.lists(st.tuples(st.booleans(), st.integers(0, 10)), min_size=1, max_size=60))
def test_concurrency_in_flight_never_exceeds_limit(steps: list[tuple[bool, int]]) -> None:
    states: dict[str, WindowState] = {}
    open_tickets: list[int] = []
    for ticket, (start, pick) in enumerate(steps):
        if start:
            if admit_all(states, [CONC], [1], 0, ticket) is None:
                open_tickets.append(ticket)
        elif open_tickets:
            refund(states, open_tickets.pop(pick % len(open_tickets)))
        assert used(states.get("conc", WindowState())) == len(open_tickets) <= CONC.limit


def test_denial_reports_when_oldest_entries_age_out() -> None:
    store = InMemoryLimitStore()
    key = LimitKey("rpm", 2, WINDOW)
    assert isinstance(_acquire(store, [key], [1], 10_000), Admit)
    assert isinstance(_acquire(store, [key], [1], 20_000), Admit)
    denial = _acquire(store, [key], [1], 30_000)
    assert denial == Deny("rpm", 40_000)
    assert isinstance(_acquire(store, [key], [1], 70_000), Admit)


def test_refund_restores_capacity() -> None:
    store = InMemoryLimitStore()
    first = _acquire(store, [RPM], [1], 0)
    assert isinstance(first, Admit)
    for _ in range(4):
        assert isinstance(_acquire(store, [RPM], [1], 0), Admit)
    assert isinstance(_acquire(store, [RPM], [1], 0), Deny)
    asyncio.run(store.release(first))
    assert isinstance(_acquire(store, [RPM], [1], 0), Admit)


def test_all_or_nothing_leaves_no_partial_admission() -> None:
    store = InMemoryLimitStore()
    first = _acquire(store, [CONC], [1], 0)
    second = _acquire(store, [CONC], [1], 0)
    assert isinstance(first, Admit)
    assert isinstance(second, Admit)
    denial = _acquire(store, [RPM, TPM, CONC], [1, 10, 1], 1_000)  # CONC is full, listed last
    assert denial == Deny("conc", 1_000)
    # RPM and TPM must be untouched: the full 5 requests / 100 tokens are still available.
    for _ in range(5):
        assert isinstance(_acquire(store, [RPM], [1], 1_000), Admit)
    assert isinstance(_acquire(store, [TPM], [100], 1_000), Admit)


@settings(max_examples=300, deadline=None)
@given(
    ops=st.lists(st.tuples(st.integers(1, 8), st.integers(1, 8), st.integers(1, 8)), max_size=30)
)
def test_all_or_nothing_property_keys_move_together(ops: list[tuple[int, int, int]]) -> None:
    keys = [LimitKey("a", 10, WINDOW), LimitKey("b", 12, WINDOW), LimitKey("c", 9, None)]
    states: dict[str, WindowState] = {}
    for ticket, amounts in enumerate(ops):
        before = {n: used(s) for n, s in states.items()}
        denial = admit_all(states, keys, list(amounts), 0, ticket)
        after = {n: used(s) for n, s in states.items()}
        if denial is None:
            assert [after[k.name] - before.get(k.name, 0) for k in keys] == list(amounts)
        else:
            assert all(after[n] == before.get(n, 0) for n in after)


def test_complete_frees_concurrency_but_keeps_rate_usage() -> None:
    store = InMemoryLimitStore()
    keys = [LimitKey("rpm", 1, WINDOW), LimitKey("conc", 1, None)]
    admit = _acquire(store, keys, [1, 1], 0)
    assert isinstance(admit, Admit)
    asyncio.run(store.complete(admit))
    assert _acquire(store, [keys[0]], [1], 1_000) == Deny("rpm", 59_000)
    assert isinstance(_acquire(store, [keys[1]], [1], 1_000), Admit)


def test_tpm_reconcile_down_frees_tokens() -> None:
    store = InMemoryLimitStore()
    admit = _acquire(store, [TPM], [80], 0)
    assert isinstance(admit, Admit)
    assert isinstance(_acquire(store, [TPM], [30], 1_000), Deny)
    asyncio.run(store.reconcile(admit, "tpm", 20))
    assert isinstance(_acquire(store, [TPM], [30], 1_000), Admit)


def test_tpm_reconcile_up_books_actual_usage_even_over_limit() -> None:
    store = InMemoryLimitStore()
    admit = _acquire(store, [TPM], [50], 0)
    assert isinstance(admit, Admit)
    asyncio.run(store.reconcile(admit, "tpm", 120))
    denial = _acquire(store, [TPM], [1], 1_000)
    assert denial == Deny("tpm", 59_000)  # the 120 aged from the original admission time
    assert isinstance(_acquire(store, [TPM], [1], 61_000), Admit)


def test_tpm_reconcile_to_zero_refunds_and_unknown_key_is_ignored() -> None:
    store = InMemoryLimitStore()
    admit = _acquire(store, [TPM], [100], 0)
    assert isinstance(admit, Admit)
    asyncio.run(store.reconcile(admit, "tpm", 0))
    asyncio.run(store.reconcile(admit, "missing", 5))
    assert isinstance(_acquire(store, [TPM], [100], 0), Admit)


def test_amount_above_limit_is_permanently_denied() -> None:
    store = InMemoryLimitStore()
    denial = _acquire(store, [TPM], [101], 0)
    assert isinstance(denial, Deny)
    assert denial.permanent
    concurrent = _acquire(store, [LimitKey("c", 1, None)], [2], 0)
    assert isinstance(concurrent, Deny)
    assert concurrent.permanent


def test_invalid_input_is_rejected() -> None:
    store = InMemoryLimitStore()
    for keys, amounts in (([RPM, RPM], [1, 1]), ([RPM], [1, 2]), ([RPM], [0])):
        try:
            _acquire(store, keys, amounts, 0)
        except ValueError:
            continue
        raise AssertionError("expected ValueError")
    try:
        asyncio.run(store.reconcile(Admit(1, (), ()), "tpm", -1))
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_concurrent_tasks_never_over_admit() -> None:
    async def run() -> int:
        store = InMemoryLimitStore()
        results = await asyncio.gather(*(store.try_acquire([RPM], [1], 0) for _ in range(50)))
        return sum(isinstance(r, Admit) for r in results)

    assert asyncio.run(run()) == RPM.limit
