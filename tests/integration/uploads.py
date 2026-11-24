"""Request bodies a test drives by hand, and the keys to send them with.

`Body.stalled` is set when the reader asks for more after the last part: by then every part was
pulled and charged to the gate, so a test can look at the gate without guessing at timing.
"""

import asyncio
from typing import Any

from spendgate.ledger.key_types import KeySpec
from tests.integration.harness import NOW_MS, Stack

MIB = 1024 * 1024
JSON_HEAD = {"content-type": "application/json"}


class Body:
    """A request body that sends `parts`, then ends or (`stall=True`) stalls forever."""

    def __init__(self, parts: list[bytes], *, stall: bool = False) -> None:
        self.parts, self.stall, self.pulled = list(parts), stall, 0
        self.stalled = asyncio.Event()

    def __aiter__(self) -> "Body":
        return self

    async def __anext__(self) -> bytes:
        if self.parts:
            part = self.parts.pop(0)
            self.pulled += len(part)
            return part
        if self.stall:
            self.stalled.set()
            await asyncio.Event().wait()
        raise StopAsyncIteration


async def read_or_refused(call: "asyncio.Future[Any]", body: Body) -> None:
    """Wait until the read stalls on `body`, or the call has answered (a refusal never stalls)."""
    stalled = asyncio.ensure_future(body.stalled.wait())
    await asyncio.wait({call, stalled}, return_when=asyncio.FIRST_COMPLETED)
    stalled.cancel()
    await asyncio.gather(stalled, return_exceptions=True)


def auth(secret: str) -> dict[str, str]:
    return {"authorization": f"Bearer {secret}"} | JSON_HEAD


def bearer(s: Stack) -> dict[str, str]:
    return auth(s.secret)


async def second_key(s: Stack) -> str:
    made = await s.keys.create_key(KeySpec(s.scope.id, "other"), actor="test", now=NOW_MS)
    return made.secret
