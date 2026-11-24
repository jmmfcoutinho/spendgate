"""Step 9 in motion: the one money transition of a request, plus what follows it.

`Closer.close` makes exactly one `release` or `settle` (retried a few times if the ledger blinks;
the sweeper is the backstop), gives the limit slots back, works out what is left of the tightest
budget and tells the observer. Buffered calls and streams close through here, so the invariant has
one home. What is left comes with the settlement (read under its locks), so a header costs no
ledger read of its own; only a settlement that brought none (not applied, a late settle, a ledger
that did not answer) falls back to a lock-free read.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from spendgate.config import Settings
from spendgate.core.limits import Admit, LimitStore
from spendgate.core.money import Money
from spendgate.core.outcomes import Outcome
from spendgate.ledger.key_types import Principal
from spendgate.ledger.port import BudgetRoom, LedgerPort, Settlement
from spendgate.pipeline import steps
from spendgate.pipeline.alerts import log_alerts
from spendgate.pipeline.model import Clock, Inbound, Observer, SettledEvent
from spendgate.pipeline.settle import Book, Closed, Decision, decide
from spendgate.pipeline.steps import Prepared

_SETTLE_TRIES = 3
_BACKOFF_SECONDS = (0.05, 0.25)  # waits between the tries of one close
_GIVE_UP_LEASES = 10  # an unsettled close is dropped this many leases after it failed
# How long a scope chain is reused. Re-parenting a scope or closing one must reach headers, the
# settled event and the stream watch's kill-switch check without a restart; half a minute keeps
# the ledger reads per request near zero under load and bounds how stale a chain can be.
CHAIN_TTL_MS = 30_000
type Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Call:
    """One admitted request after step 6."""

    id: str
    inbound: Inbound
    principal: Principal
    prepared: Prepared
    admit: Admit | None
    started: int


class Closer:
    def __init__(  # noqa: PLR0913, PLR0917 - the closer's collaborators
        self, settings: Settings, ledger: LedgerPort, limits: LimitStore, observer: Observer,
        now: Clock, sleep: Sleeper = asyncio.sleep,
    ) -> None:  # fmt: skip
        self._settings, self._ledger, self._limits = settings, ledger, limits
        self._observer, self._now, self._sleep = observer, now, sleep
        # Closes the ledger did not take: (decision, when it first failed). Each heartbeat retries
        # them; their holds are no longer renewed, so the sweeper charges them in full (CR-02).
        self._unsettled: dict[str, tuple[Decision, int]] = {}
        self._chains: dict[str, tuple[int, tuple[tuple[str, str], ...]]] = {}  # (read at, links)

    async def close(
        self, call: Call, book: Book, interrupted: Outcome | None, ttft_ms: int | None = None
    ) -> Closed:
        """Exactly one release or settle, then limits, budget read and the observer event."""
        decision = decide(book, interrupted, self._now())
        done = await self._book(call.id, decision)
        if done is None:
            self._unsettled[call.id] = (decision, self._now())
        await self._finish_limits(call, book, decision)
        left = await self.left_after(call.principal.scope_id, done, book.budget_ids)
        self._settled(call, book, decision, left, ttft_ms)
        return Closed(decision, done is not None, left)

    async def release_unsent(self, request_id: str, book: Book) -> None:
        """Best-effort free end of a hold that may exist though nothing was sent (NF-03).

        State-guarded, so harmless when the hold never committed. If the ledger will not take it
        now, the heartbeat retries before the lease runs out.
        """
        decision = decide(book, Outcome.CANCELLED, self._now())
        if await self._book(request_id, decision, tries=1) is None:
            self._unsettled[request_id] = (decision, self._now())

    async def _book(
        self, request_id: str, decision: Decision, tries: int = _SETTLE_TRIES
    ) -> Settlement | None:
        """The one transition; None when the ledger did not take it."""
        for attempt in range(tries):
            if attempt:
                await self._sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS)) - 1])
            try:
                if decision.release:
                    return await self._ledger.release(request_id, decision.attempts, self._now())
                done = await self._ledger.settle(
                    request_id, decision.charge.nano, decision.attempts, self._now()
                )
            except Exception:  # noqa: S112 - tried again; the sweeper is the backstop
                continue
            log_alerts(done.alerts, self._settings.ledger_currency)  # a breach, a block
            return done
        return None

    def unsettled(self) -> frozenset[str]:
        """Requests whose one money transition the ledger has not yet taken."""
        return frozenset(self._unsettled)

    async def retry_unsettled(self) -> None:
        """One more try for each close the ledger refused; a hold nobody settles is swept."""
        give_up = _GIVE_UP_LEASES * self._settings.lease_ms
        for rid, (decision, since) in list(self._unsettled.items()):
            booked = await self._book(rid, decision, tries=1) is not None
            if booked or self._now() - since > give_up:
                self._unsettled.pop(rid, None)

    async def _finish_limits(self, call: Call, book: Book, decision: Decision) -> None:
        if call.admit is None:
            return
        with contextlib.suppress(Exception):
            if decision.release:
                await self._limits.release(call.admit)
                return
            await self._limits.complete(call.admit)
            if book.usage is not None:
                actual = book.usage.input_tokens + book.usage.output_tokens
                await self._limits.reconcile(
                    call.admit, steps.tpm_key(call.principal.key_id), actual
                )

    async def free_limits(self, admit: Admit | None) -> None:
        if admit is not None:
            with contextlib.suppress(Exception):
                await self._limits.release(admit)

    def _settled(
        self, call: Call, book: Book, decision: Decision, left: tuple[Money, str] | None,
        ttft_ms: int | None,
    ) -> None:  # fmt: skip
        now = self._now()
        cached = self._chains.get(call.principal.scope_id)  # just refreshed by `remaining`
        chain = cached[1] if cached else None
        event = SettledEvent(
            call.id, call.principal.key_id, call.principal.scope_id, call.inbound.endpoint.path,
            call.prepared.route.catalog_id, decision.deployment, decision.outcome,
            decision.charge, book.pricing.worst, book.usage, max(now - call.started, 0),
            decision.release, left[0] if left else None, chain[0][1] if chain else None,
            any(a.review_required for a in decision.attempts), ttft_ms,
            decision.attempts[-1].http_status if decision.attempts else None,
        )  # fmt: skip
        with contextlib.suppress(Exception):  # observers never affect money handling
            self._observer.on_settled(event)

    async def remaining(
        self, scope_id: str, budget_ids: frozenset[str] | None = None
    ) -> tuple[Money, str] | None:
        """Room left in the tightest budget on the key's scope chain, best effort (headers only).

        `budget_ids` limits it to the budgets the call's hold took, so a budget filtered to another
        model is never reported as this call's remainder.
        """
        try:
            chain = await self._chain(scope_id)
            now = self._now()
            best: tuple[int, str] | None = None
            for sid, path in chain:
                for row in await self._ledger.totals(sid, now):
                    if budget_ids is not None and row.budget_id not in budget_ids:
                        continue
                    room = max(row.effective_cap_nano - row.spent_nano - row.held_nano, 0)
                    if best is None or room < best[0]:
                        best = (room, path)
        except Exception:
            return None
        return (Money(best[0], self._settings.ledger_currency), best[1]) if best else None

    async def left_after(
        self, scope_id: str, done: Settlement | None, budget_ids: frozenset[str] | None
    ) -> tuple[Money, str] | None:
        """The header after a close: from the settlement's rooms when they cover every budget the
        hold took, else (not applied, late, a period boundary since the hold, a disabled budget, a
        ledger that did not answer) the lock-free read."""
        rooms = done.rooms if done is not None else ()
        if budget_ids is not None and budget_ids <= {r.budget_id for r in rooms}:
            return await self.room_left(scope_id, rooms, budget_ids)
        return await self.remaining(scope_id, budget_ids)  # best effort

    async def room_left(
        self,
        scope_id: str,
        rooms: tuple[BudgetRoom, ...],
        budget_ids: frozenset[str] | None = None,
    ) -> tuple[Money, str] | None:
        """`remaining` from the rooms a hold or settlement returned: no ledger read but the chain.

        Only budgets on the key's chain count (a session's own budget is not), the tightest wins,
        and on a tie the scope nearest the key, as in `remaining`.
        """
        try:
            chain = await self._chain(scope_id)
        except Exception:
            return None
        depth = {sid: (n, path) for n, (sid, path) in enumerate(chain)}
        best: tuple[int, int, str] | None = None
        for r in rooms:
            if r.scope_id not in depth or (
                budget_ids is not None and r.budget_id not in budget_ids
            ):
                continue
            n, path = depth[r.scope_id]
            candidate = (max(r.room_nano, 0), n, path)
            best = candidate if best is None else min(best, candidate)
        return (Money(best[0], self._settings.ledger_currency), best[2]) if best else None

    async def chain_scope_ids(self, scope_id: str) -> tuple[str, ...]:
        """The scope and its ancestors, as kill switches see them."""
        return tuple(sid for sid, _ in await self._chain(scope_id))

    async def _chain(self, scope_id: str) -> tuple[tuple[str, str], ...]:
        """The (id, path) links from the scope up to its root, read again after `CHAIN_TTL_MS`."""
        now = self._now()
        cached = self._chains.get(scope_id)
        if cached is not None and now - cached[0] < CHAIN_TTL_MS:
            return cached[1]
        links: list[tuple[str, str]] = []
        current: str | None = scope_id
        while current is not None:
            row = await self._ledger.get_scope(current)
            if row is None:
                break
            links.append((row.id, row.path))
            current = row.parent_id
        self._chains[scope_id] = (now, tuple(links))
        return self._chains[scope_id][1]
