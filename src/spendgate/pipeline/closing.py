"""Step 9 in motion: the one money transition of a request, plus what follows it.

`Closer.close` makes exactly one `release` or `settle` (retried a few times if the ledger blinks;
the sweeper is the backstop), gives the limit slots back, reads what is left of the tightest budget
and tells the observer. Buffered calls and streams close through here, so the invariant has one
home.
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
from spendgate.ledger.port import LedgerPort
from spendgate.pipeline import steps
from spendgate.pipeline.model import Clock, Inbound, Observer, SettledEvent
from spendgate.pipeline.settle import Book, Closed, Decision, decide
from spendgate.pipeline.steps import Prepared

_SETTLE_TRIES = 3
_BACKOFF_SECONDS = (0.05, 0.25)  # waits between the tries of one close
_GIVE_UP_LEASES = 10  # an unsettled close is dropped this many leases after it failed
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
        self._chains: dict[str, tuple[tuple[str, str], ...]] = {}

    async def close(
        self, call: Call, book: Book, interrupted: Outcome | None, ttft_ms: int | None = None
    ) -> Closed:
        """Exactly one release or settle, then limits, budget read and the observer event."""
        decision = decide(book, interrupted, self._now())
        booked = await self._book(call.id, decision)
        if not booked:
            self._unsettled[call.id] = (decision, self._now())
        await self._finish_limits(call, book, decision)
        left = await self.remaining(call.principal.scope_id)  # best effort; also fills the chain
        self._settled(call, book, decision, left, ttft_ms)
        return Closed(decision, booked, left)

    async def release_unsent(self, request_id: str, book: Book) -> None:
        """Best-effort free end of a hold that may exist though nothing was sent (NF-03).

        State-guarded, so harmless when the hold never committed. If the ledger will not take it
        now, the heartbeat retries before the lease runs out.
        """
        decision = decide(book, Outcome.CANCELLED, self._now())
        if not await self._book(request_id, decision, tries=1):
            self._unsettled[request_id] = (decision, self._now())

    async def _book(self, request_id: str, decision: Decision, tries: int = _SETTLE_TRIES) -> bool:
        for attempt in range(tries):
            if attempt:
                await self._sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS)) - 1])
            try:
                if decision.release:
                    await self._ledger.release(request_id, decision.attempts, self._now())
                else:
                    await self._ledger.settle(
                        request_id, decision.charge.nano, decision.attempts, self._now()
                    )
                return True
            except Exception:  # noqa: S112 - tried again; the sweeper is the backstop
                continue
        return False

    def unsettled(self) -> frozenset[str]:
        """Requests whose one money transition the ledger has not yet taken."""
        return frozenset(self._unsettled)

    async def retry_unsettled(self) -> None:
        """One more try for each close the ledger refused; a hold nobody settles is swept."""
        give_up = _GIVE_UP_LEASES * self._settings.lease_ms
        for rid, (decision, since) in list(self._unsettled.items()):
            if await self._book(rid, decision, tries=1) or self._now() - since > give_up:
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
        chain = self._chains.get(call.principal.scope_id)
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

    async def remaining(self, scope_id: str) -> tuple[Money, str] | None:
        """Room left in the tightest budget on the key's scope chain, best effort (headers only)."""
        try:
            chain = await self._chain(scope_id)
            now = self._now()
            best: tuple[int, str] | None = None
            for sid, path in chain:
                for row in await self._ledger.totals(sid, now):
                    room = max(row.effective_cap_nano - row.spent_nano - row.held_nano, 0)
                    if best is None or room < best[0]:
                        best = (room, path)
        except Exception:
            return None
        return (Money(best[0], self._settings.ledger_currency), best[1]) if best else None

    async def chain_scope_ids(self, scope_id: str) -> tuple[str, ...]:
        """The scope and its ancestors, as kill switches see them."""
        return tuple(sid for sid, _ in await self._chain(scope_id))

    async def _chain(self, scope_id: str) -> tuple[tuple[str, str], ...]:
        if scope_id not in self._chains:
            links: list[tuple[str, str]] = []
            current: str | None = scope_id
            while current is not None:
                row = await self._ledger.get_scope(current)
                if row is None:
                    break
                links.append((row.id, row.path))
                current = row.parent_id
            self._chains[scope_id] = tuple(links)
        return self._chains[scope_id]
