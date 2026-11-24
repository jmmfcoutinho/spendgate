"""Credential refreshes off the event loop, one at a time per credential.

Token sources (Entra ID, google-auth, the AWS credential chain) refresh with blocking network
calls of 100 ms to several seconds. Made on the event loop, one refresh stalls every request the
process is serving. `Refreshes` runs the blocking call in a worker thread and holds an
`asyncio.Lock` per credential, so N requests that find the same token stale wait for one refresh
instead of starting N.
"""

import asyncio
from collections.abc import Callable


class Refreshes:
    """Per-credential locks for `Provider.prepare`; one instance per adapter instance."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}  # keyed by a scope or digest, never a secret

    async def ensure(
        self, key: str, stale: Callable[[], bool], refresh: Callable[[], object]
    ) -> None:
        """Run `refresh` in a worker thread when `stale()` holds, one at a time per `key`.

        `stale` runs on the event loop, so it must only read a cache. It is asked again under the
        lock: a caller that waited finds the token the first caller fetched. A raise from
        `refresh` propagates (fail closed); nothing is cached, so the next caller tries again.
        """
        if not stale():
            return
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if stale():
                await asyncio.to_thread(refresh)
