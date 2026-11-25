"""A flood of distinct unknown keys: what it costs, pinned (SR-11, accepted risk R-06).

There is no per-peer throttle on failed authentication (see R-06 in docs/threat-model.md for
why). The risk is accepted because the cost of each such request is bounded and small: one
indexed read of the `keys` table by prefix, nothing else. No body byte is read, no gate place or
intake ticket is taken, nothing is held or written, and nothing reaches a provider. The negative
cache stays bounded however many prefixes arrive (T-AUTH-05). If a change makes a failed key cost
more than this, this test fails and the acceptance must be revisited.
"""

import secrets
from pathlib import Path

import pytest

from spendgate.ledger.key_types import Principal, StoredKey
from spendgate.pipeline.model import CHAT
from tests.integration.harness import async_test, open_stack
from tests.integration.uploads import MIB, Body, auth

FLOOD = 300


def fake_key() -> str:
    """Well formed, so it passes the shape check and costs a lookup; never a real key."""
    return f"sg-{secrets.token_hex(6)}-{'0' * 48}"


@async_test
async def test_t_auth_06_a_flood_of_distinct_unknown_keys_costs_one_read_each_and_nothing_else(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path) as s:
        reads: list[str] = []
        find_key = s.ledger.find_key

        async def counted(prefix: str) -> StoredKey | None:
            reads.append(prefix)
            return await find_key(prefix)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(s.ledger, "find_key", counted)
            amplifying = b"[" + b"{}," * MIB  # 3 MiB of JSON (one copy, shared by every body)
            bodies = []
            for _ in range(FLOOD):
                body = Body([amplifying])
                headers = auth(fake_key()) | {"content-length": str(3 * MIB + 1)}
                reply = await s.pipeline.serve(CHAT, headers, body)
                assert reply.status == 401
                bodies.append(body)
            assert len(reads) == len(set(reads)) == FLOOD  # one read per distinct key, no more
            assert all(b.pulled == 0 for b in bodies)  # no body byte was read
            assert s.pipeline.gate.in_flight == 0 and s.pipeline.gate.buffered_bytes == 0
            assert await s.totals() == (0, 0)
            assert s.observer.held == [] and s.observer.attempts == []
            s.assert_no_contact()
            assert isinstance(await s.keys.verify_api(s.secret, s.clock.now()), Principal)
