"""Why an open stream must stop after it started (protocol 2.8): a kill switch, a session's clock.

Admission checked both once. A stream can run for minutes, so the heartbeat asks again with the
facts recorded when the stream opened. The decision is pure; `Leases` applies it by aborting the
stream, which then settles like any other abort (usage if seen, else the full hold).

The facts are read from the ledger after the hold, before anything is sent. A stream whose facts
cannot be read is never run unwatched (M-12, T-BLK-07): the read is tried once more at once, both
tries inside the hold's deadline, and if that fails too the call ends `ledger_unavailable`.
"""

import asyncio
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass

from spendgate.core.failures import StopReason
from spendgate.core.policy import KeyPolicy, KillSwitch, matching_kill_switch
from spendgate.ledger.port import Held, MoneyLedger
from spendgate.pipeline.closing import Call
from spendgate.pipeline.faults import Fault, InternalEvent, fault_of
from spendgate.pipeline.model import Observer, report_internal

_MS_PER_S = 1000
WATCH_FAULT = "pipeline.watch"  # the tag on a failed facts read in the internal-error log


@dataclass(frozen=True, slots=True)
class StreamFacts:
    """What the admission rules need again for one open stream."""

    key: KeyPolicy
    chain_scope_ids: tuple[str, ...]  # the key's scope and its ancestors (as at admission)
    model: str  # catalog id
    provider: str  # the one serving it: a provider switch on it stops the stream
    session_ends_at: int | None  # epoch ms after which the session's `max_seconds` has passed
    key_lineage: tuple[str, ...] = ()  # keys this key replaced (as at admission; never changes)


def stop_reason(
    facts: StreamFacts, switches: Collection[KillSwitch], now: int
) -> StopReason | None:
    """`kill_switch`, `session_time`, or None while the stream may go on."""
    if matching_kill_switch(
        switches,
        facts.key,
        facts.chain_scope_ids,
        facts.model,
        (facts.provider,),
        key_lineage=facts.key_lineage,
    ):
        return StopReason.KILL_SWITCH
    if facts.session_ends_at is not None and now > facts.session_ends_at:
        return StopReason.SESSION_TIME  # same strict comparison as admission (`core.sessions`)
    return None


async def session_ends_at(ledger: MoneyLedger, held: Held) -> int | None:
    """When the held call's session runs out of wall-clock time, if it has such a limit."""
    if held.session_scope_id is None:
        return None
    row = await ledger.get_scope(held.session_scope_id)
    if row is None or row.started_at is None or row.max_seconds is None:
        return None
    return row.started_at + row.max_seconds * _MS_PER_S


async def facts_at_open(
    call: Call,
    held: Held,
    chain_of: Callable[[str], Awaitable[tuple[str, ...]]],
    ledger: MoneyLedger,
) -> StreamFacts:
    """One read of what the watch needs: the key's scope chain and its session's clock."""
    chain = await chain_of(call.principal.scope_id)
    ends = await session_ends_at(ledger, held)
    model = call.prepared.route.catalog_id
    return StreamFacts(call.principal.policy, chain, model, "", ends, held.key_lineage)


async def read_facts(
    read: Callable[[], Awaitable[StreamFacts]],
    deadline_s: float,
    observer: Observer,
    request_id: str,
) -> StreamFacts | Fault:
    """The facts, or the redacted trace of the failed read: the caller then fails closed.

    Tried twice with no wait between (a blink is retried at once); one deadline bounds both, so a
    stuck ledger costs the call at most what a stuck hold would. A failed first try is logged as
    an internal error at once, else it would leave no trace when the second one works; the last
    failure is the caller's to report, once it knows whether the call was charged.
    """
    try:
        async with asyncio.timeout(deadline_s):
            first = await _try(read)
            if not isinstance(first, Fault):
                return first
            report_internal(observer, InternalEvent(request_id, first))
            return await _try(read)
    except TimeoutError as exc:  # the deadline; a read's own errors are caught in `_try`
        return fault_of(WATCH_FAULT, exc)


async def _try(read: Callable[[], Awaitable[StreamFacts]]) -> StreamFacts | Fault:
    try:
        return await read()
    except Exception as exc:
        return fault_of(WATCH_FAULT, exc)
