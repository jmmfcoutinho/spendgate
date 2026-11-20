"""Lease bookkeeping for this instance's open streams.

The heartbeat renews every lease of the instance with one statement. A stream whose id the renewal
did not return was expired by a sweeper, and a failed renewal means nobody knows: both abort the
stream. Independently, a stream whose last known lease is within one heartbeat of expiry stops
itself at its next step (the heartbeat itself may be stuck). Aborting means: close the upstream
call and settle what was seen (the full hold if no usage).

The heartbeat also enforces, on its own, what a stream would otherwise only notice when its client
pulls the next chunk (NF-02): the stream deadline, kill switch, session time and lease loss. A
stream so stopped is no longer renewed and `abort` settles it even if nobody reads it again.
"""

from collections.abc import Collection
from typing import Protocol

from spendgate.core.policy import KillSwitch
from spendgate.pipeline.watch import StreamFacts, stop_reason


class Abortable(Protocol):
    def abort(self, reason: str) -> None: ...

    async def aclose(self) -> None: ...


class Leases:
    def __init__(self, lease_ms: int, heartbeat_ms: int) -> None:
        self._lease_ms, self._heartbeat_ms = lease_ms, heartbeat_ms
        self._until: dict[str, int] = {}
        self._streams: dict[str, Abortable] = {}
        self._facts: dict[str, StreamFacts] = {}
        self._deadline: dict[str, int] = {}
        self._stopping: set[str] = set()

    def register(
        self, rid: str, held_at: int, stream: Abortable, deadline: int | None = None
    ) -> None:
        """A stream opened; its lease runs `lease_ms` from the hold, the stream until `deadline`."""
        self._until[rid] = held_at + self._lease_ms
        self._streams[rid] = stream
        if deadline is not None:
            self._deadline[rid] = deadline

    def _stop(self, rid: str, reason: str) -> None:
        self._stopping.add(rid)  # never renewed again, whatever the settlement takes
        self._streams[rid].abort(reason)

    def expire(self, now: int) -> None:
        """Stop every open stream that outlived its deadline, read or not (SR-13, NF-02)."""
        for rid, deadline in list(self._deadline.items()):
            if now >= deadline and rid in self._streams:
                self._stop(rid, "max_duration")

    def stopping(self) -> frozenset[str]:
        """Streams being stopped: their holds are settled, not renewed."""
        return frozenset(self._stopping)

    def watch(self, rid: str, facts: StreamFacts) -> None:
        """Remember what a kill switch or session clock is checked against for this stream."""
        self._facts[rid] = facts

    def watching(self) -> bool:
        return bool(self._facts)

    def stop_forbidden(self, switches: Collection[KillSwitch], now: int) -> None:
        """Abort every open stream a switch in force or its session's time limit now forbids."""
        for rid, facts in list(self._facts.items()):
            reason = stop_reason(facts, switches, now)
            if reason is not None and rid in self._streams:
                self._stop(rid, reason)

    def unregister(self, rid: str) -> None:
        self._deadline.pop(rid, None)
        self._stopping.discard(rid)
        self._facts.pop(rid, None)
        self._until.pop(rid, None)
        self._streams.pop(rid, None)

    def watched(self) -> frozenset[str]:
        """The streams open now: take this BEFORE a renewal starts (a hold committed while it ran
        may be missing from its result without being lost)."""
        return frozenset(self._streams.keys() - self._stopping)

    def renewed(self, now: int, watched: frozenset[str], renewed: frozenset[str]) -> None:
        """A renewal that started at `now` returned `renewed`; `watched` was open before it."""
        for rid in watched & self._streams.keys():
            if rid in renewed:
                self._until[rid] = now + self._lease_ms
            else:
                self._stop(rid, "lease_lost")

    def failed(self, watched: frozenset[str]) -> None:
        for rid in watched & self._streams.keys():
            self._stop(rid, "renewal_failed")

    def near_expiry(self, rid: str, now: int) -> bool:
        until = self._until.get(rid)
        return until is not None and until - now < self._heartbeat_ms

    def open(self) -> list[Abortable]:
        return list(self._streams.values())
