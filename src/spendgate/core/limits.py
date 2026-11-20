"""Per-process rate, token and concurrency limits.

Time is integer epoch milliseconds throughout, like the rest of ``spendgate.core``.

Limits are enforced per process in v0.1: a multi-instance deployment divides each limit by the
number of instances. Budgets stay exact in the ledger; a shared limit store is on the roadmap.

Algorithm: a sliding-window *log* (one entry per admission, summed over the trailing window)
rather than a bucketed counter or GCRA. Summing real entries makes "admitted <= limit in every
window of the given length" exactly true, not approximately, and the log is bounded by the limit
because every entry carries at least one unit. Concurrency uses the same log without expiry:
entries leave only when the request completes or is refunded.

The pure functions take and mutate plain ``WindowState`` values and never read a clock; the
async ``InMemoryLimitStore`` adds a lock and ticket ids around them.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

CONCURRENCY_RETRY_HINT_MS = 1_000


@dataclass(frozen=True, slots=True)
class LimitKey:
    """One limit to enforce: ``window_ms=None`` means an in-flight concurrency limit."""

    name: str
    limit: int
    window_ms: int | None = 60_000


@dataclass(slots=True)
class Entry:
    """One admission recorded against a key."""

    ticket: int
    at: int
    amount: int


@dataclass(slots=True)
class WindowState:
    """Admissions currently counted against one key, oldest first."""

    entries: list[Entry] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Admit:
    """All keys were admitted; hand this back to ``release``, ``complete`` or ``reconcile``."""

    ticket: int
    keys: tuple[LimitKey, ...]
    amounts: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Deny:
    """A key refused the request. Unify with the shared error type when core/errors lands."""

    key: str
    retry_after_ms: int
    permanent: bool = False


def _prune(state: WindowState, key: LimitKey, now: int) -> None:
    """Drop entries that left the window; concurrency entries never expire."""
    if key.window_ms is None:
        return
    cutoff = now - key.window_ms
    state.entries = [e for e in state.entries if e.at > cutoff]


def used(state: WindowState) -> int:
    """Units currently counted against the key."""
    return sum(e.amount for e in state.entries)


def check(state: WindowState, key: LimitKey, amount: int, now: int) -> Deny | None:
    """Return ``None`` when ``amount`` fits under the limit now, else the denial."""
    _prune(state, key, now)
    if amount > key.limit:
        # Can never fit, so a retry hint would only invite a retry loop.
        return Deny(key.name, key.window_ms or CONCURRENCY_RETRY_HINT_MS, permanent=True)
    excess = used(state) + amount - key.limit
    if excess <= 0:
        return None
    if key.window_ms is None:
        return Deny(key.name, CONCURRENCY_RETRY_HINT_MS)
    freed = 0
    for entry in state.entries:  # oldest first: wait until enough of them age out
        freed += entry.amount
        if freed >= excess:
            return Deny(key.name, max(entry.at + key.window_ms - now, 1))
    # Unreachable (amount <= limit means the entries always cover the excess); fail closed anyway.
    return Deny(key.name, key.window_ms)  # pragma: no cover


def admit_all(
    states: dict[str, WindowState],
    keys: Sequence[LimitKey],
    amounts: Sequence[int],
    now: int,
    ticket: int,
) -> Deny | None:
    """Admit on every key or on none.

    All keys are checked before any is written, so a late denial never leaves a partial
    admission behind (the same outcome as LiteLLM's admit-then-refund, without the undo).
    """
    if len(keys) != len(amounts) or len({k.name for k in keys}) != len(keys):
        raise ValueError("keys must be unique and match amounts one-to-one")
    if any(a < 1 for a in amounts):
        raise ValueError("amounts must be positive")
    for key, amount in zip(keys, amounts, strict=True):
        denial = check(states.setdefault(key.name, WindowState()), key, amount, now)
        if denial is not None:
            return denial
    for key, amount in zip(keys, amounts, strict=True):
        states[key.name].entries.append(Entry(ticket, now, amount))
    return None


def refund(states: dict[str, WindowState], ticket: int, names: Sequence[str] | None = None) -> None:
    """Remove a ticket's entries (all keys, or only ``names``) so the capacity comes back."""
    for name, state in states.items():
        if names is None or name in names:
            state.entries = [e for e in state.entries if e.ticket != ticket]


def adjust(state: WindowState, ticket: int, actual: int) -> None:
    """Set a ticket's amount to ``actual``; actual usage is a fact, so it may exceed the limit."""
    for entry in state.entries:
        if entry.ticket == ticket:
            entry.amount = actual
            return


class LimitStore(Protocol):
    """Async storage port for limits; the in-memory implementation below is the v0.1 one."""

    async def try_acquire(
        self, keys: Sequence[LimitKey], amounts: Sequence[int], now: int
    ) -> Admit | Deny:
        """Admit all keys or none."""
        ...

    async def release(self, admit: Admit) -> None:
        """Refund every key of an admission (the request was refused later or never sent)."""
        ...

    async def complete(self, admit: Admit) -> None:
        """Free concurrency slots; rate and token usage stay counted until the window passes."""
        ...

    async def reconcile(self, admit: Admit, key_name: str, actual: int) -> None:
        """Replace the reserved estimate on one key (TPM) with the actual amount."""
        ...


class InMemoryLimitStore:
    """Single-process store; the lock makes check-then-write atomic across awaiting tasks."""

    def __init__(self) -> None:
        self._states: dict[str, WindowState] = {}
        self._lock = asyncio.Lock()
        self._next_ticket = 0

    async def try_acquire(
        self, keys: Sequence[LimitKey], amounts: Sequence[int], now: int
    ) -> Admit | Deny:
        """Admit all keys or none."""
        async with self._lock:
            self._next_ticket += 1
            ticket = self._next_ticket
            denial = admit_all(self._states, keys, amounts, now, ticket)
            if denial is not None:
                return denial
            return Admit(ticket, tuple(keys), tuple(amounts))

    async def release(self, admit: Admit) -> None:
        """Refund every key of an admission."""
        async with self._lock:
            refund(self._states, admit.ticket)

    async def complete(self, admit: Admit) -> None:
        """Free concurrency slots only."""
        async with self._lock:
            names = [k.name for k in admit.keys if k.window_ms is None]
            refund(self._states, admit.ticket, names)

    async def reconcile(self, admit: Admit, key_name: str, actual: int) -> None:
        """Replace the reserved estimate on one key with the actual amount."""
        if actual < 0:
            raise ValueError("actual must not be negative")
        async with self._lock:
            state = self._states.get(key_name)
            if state is None:
                return
            if actual == 0:
                refund(self._states, admit.ticket, [key_name])
            else:
                adjust(state, admit.ticket, actual)
