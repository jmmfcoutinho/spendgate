"""Why an open stream must stop after it started (protocol 2.8): a kill switch, a session's clock.

Admission checked both once. A stream can run for minutes, so the heartbeat asks again with the
facts recorded when the stream opened. The decision is pure; `Leases` applies it by aborting the
stream, which then settles like any other abort (usage if seen, else the full hold).
"""

from collections.abc import Collection
from dataclasses import dataclass

from spendgate.core.failures import StopReason
from spendgate.core.policy import KeyPolicy, KillSwitch, matching_kill_switch
from spendgate.ledger.port import Held, LedgerPort

_MS_PER_S = 1000


@dataclass(frozen=True, slots=True)
class StreamFacts:
    """What the admission rules need again for one open stream."""

    key: KeyPolicy
    chain_scope_ids: tuple[str, ...]  # the key's scope and its ancestors (as at admission)
    model: str  # catalog id
    provider: str  # the one serving it: a provider switch on it stops the stream
    session_ends_at: int | None  # epoch ms after which the session's `max_seconds` has passed


def stop_reason(
    facts: StreamFacts, switches: Collection[KillSwitch], now: int
) -> StopReason | None:
    """`kill_switch`, `session_time`, or None while the stream may go on."""
    if matching_kill_switch(
        switches, facts.key, facts.chain_scope_ids, facts.model, (facts.provider,)
    ):
        return StopReason.KILL_SWITCH
    if facts.session_ends_at is not None and now > facts.session_ends_at:
        return StopReason.SESSION_TIME  # same strict comparison as admission (`core.sessions`)
    return None


async def session_ends_at(ledger: LedgerPort, held: Held) -> int | None:
    """When the held call's session runs out of wall-clock time, if it has such a limit."""
    if held.session_scope_id is None:
        return None
    row = await ledger.get_scope(held.session_scope_id)
    if row is None or row.started_at is None or row.max_seconds is None:
        return None
    return row.started_at + row.max_seconds * _MS_PER_S
