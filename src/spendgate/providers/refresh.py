"""Credential refreshes off the event loop, one in flight per credential.

Token sources (Entra ID, google-auth, the AWS credential chain) refresh with blocking network
calls of 100 ms to several seconds. Made on the event loop, one refresh stalls every request the
process is serving. `Refreshes` runs the blocking call in a worker thread, and every caller that
finds the same credential stale awaits that one refresh instead of starting its own.
"""

import asyncio
from collections.abc import Callable


class Refreshes:
    """Single-flight refreshes for `Provider.prepare`; one instance per adapter instance."""

    def __init__(self) -> None:
        # One in-flight refresh per key (a scope or digest, never a secret), dropped when done.
        self._flights: dict[str, asyncio.Future[object]] = {}

    async def ensure(
        self, key: str, stale: Callable[[], bool], refresh: Callable[[], object]
    ) -> None:
        """Run `refresh` in a worker thread when `stale()` holds, at most one at a time per `key`.

        `stale` runs on the event loop, so it must only read a cache. Callers that find a refresh
        in flight await it. The refresh is shielded: a cancelled caller (a client that left) does
        not end it, and the next caller joins it instead of starting a second worker thread. A
        raise from `refresh` reaches every waiting caller (fail closed); nothing is cached, so
        the next call after it tries again.
        """
        if not stale():
            return
        flight = self._flights.get(key)
        if flight is None or flight.get_loop() is not asyncio.get_running_loop():
            flight = asyncio.ensure_future(asyncio.to_thread(refresh))
            self._flights[key] = flight
            flight.add_done_callback(lambda done: self._land(key, done))
        await asyncio.shield(flight)

    def _land(self, key: str, done: asyncio.Future[object]) -> None:
        if self._flights.get(key) is done:
            del self._flights[key]
        if not done.cancelled():
            done.exception()  # retrieved: a failure nobody awaits any more is no loop warning
