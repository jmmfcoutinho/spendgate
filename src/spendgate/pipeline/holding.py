"""Step 7 in motion: the admission transaction, bounded, shielded, timed and traced.

`Holder.hold` waits at most `hold_seconds` for the ledger's hold, then refuses with
`ledger_unavailable` and nothing is sent (ADR-0012). The transaction runs in its own task, so
neither a client cancel nor the deadline can strand a committed hold: whatever commits after the
caller stopped waiting is released at 0, as nothing was sent.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace

from spendgate.core.errors import ErrorCode, Refusal
from spendgate.ledger.port import ConflictError, Held, HoldRequest, MoneyLedger, Refused
from spendgate.pipeline import answers, tracing
from spendgate.pipeline.closing import Call, Closer
from spendgate.pipeline.model import Clock, Observer, report_ledger
from spendgate.pipeline.settle import Book

_LEDGER_RETRY_SECONDS = 5
# How long a call waits for its hold. Above the Postgres lock and statement timeouts (2 s, 5 s),
# so those usually answer first; this catches the rest (pool wait, slow server, SQLite busy).
HOLD_SECONDS = 10.0
LEDGER_DOWN = Refusal(
    ErrorCode.LEDGER_UNAVAILABLE,
    "The ledger is unavailable; try again.",
    retry_after_seconds=_LEDGER_RETRY_SECONDS,
)
type Spawn = Callable[[Awaitable[None]], object]


class Holder:
    def __init__(  # noqa: PLR0913, PLR0917 - the pipeline's own collaborators, passed through
        self, ledger: MoneyLedger, closer: Closer, observer: Observer, tracer: tracing.Tracer,
        now: Clock, spawn: Spawn, hold_seconds: float = HOLD_SECONDS,
    ) -> None:  # fmt: skip
        self._ledger, self._closer, self._observer, self._tracer = ledger, closer, observer, tracer
        self._now, self._spawn, self._hold_s = now, spawn, hold_seconds

    async def hold(self, request: HoldRequest, call: Call, book: Book) -> Held | Refusal:
        """The hold, or why not: a budget refusal, or `ledger_unavailable` / internal on a fault."""
        started = self._now()
        with self._tracer.span(tracing.HOLD, call.trace, call.tags) as span:
            held = await self._hold(request, call, book)
            span.set("spendgate.outcome", "held" if isinstance(held, Held) else held.code.value)
        ok = held is not LEDGER_DOWN and held is not answers.INTERNAL  # a refusal is an answer
        report_ledger(self._observer, "hold", started, self._now(), ok=ok)
        return held

    async def _hold(self, request: HoldRequest, call: Call, book: Book) -> Held | Refusal:
        task = asyncio.ensure_future(self._ledger.hold(request))
        deadline = asyncio.timeout(self._hold_s)
        try:
            async with deadline:
                result = await asyncio.shield(task)
        except asyncio.CancelledError:
            self._spawn(self._give_back(task, call, book))  # before the upstream call: released
            raise
        except ConflictError:
            return answers.INTERNAL
        except Exception:
            if deadline.expired():
                # Too slow: refuse now. The task runs on, bounded by the ledger's own timeouts,
                # and whatever it commits is released at 0; the caller frees the limits.
                self._spawn(self._give_back(task, replace(call, admit=None), book))
            else:  # the transaction may have committed: end it free, never swept at full
                await self._closer.release_unsent(call.id, book)
            return LEDGER_DOWN
        return result.refusal if isinstance(result, Refused) else result

    async def _give_back(
        self, task: "asyncio.Future[Held | Refused]", call: Call, book: Book
    ) -> None:
        """A cancel or the deadline cut the hold short: if it may commit, release it (unsent)."""
        try:
            result = await task
        except Exception:
            result = None  # ambiguous: release is state-guarded, harmless if nothing committed
        if result is None or isinstance(result, Held):
            await self._closer.release_unsent(call.id, book)
        await self._closer.free_limits(call.admit)
