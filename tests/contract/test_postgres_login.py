"""Postgres login per connection: an IAM token source reaches every new connection.

The local test server trusts loopback, so a password is accepted whatever it is; what this test
checks is that the pool asks for one each time it connects.
"""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from spendgate.ledger.postgres import PostgresLedger
from tests.contract.rig import workdir_location

pytestmark = pytest.mark.postgres

TOKEN = "fake-iam-token-0000"  # noqa: S105  # pragma: allowlist secret


@pytest.fixture
def location(postgres_url: str, tmp_path: Path) -> Iterator[str]:
    yield workdir_location("postgres", tmp_path)


async def _connections(ledger: PostgresLedger, n: int) -> list[str]:
    """Open `n` pooled connections at once; the password each one logged in with."""
    pool = await ledger._ensure_pool()
    conns = [await pool.getconn() for _ in range(n)]
    try:
        return [c.info.password or "" for c in conns]
    finally:
        for c in conns:
            await pool.putconn(c)


def test_open_and_every_new_pooled_connection_log_in_with_the_current_token(
    location: str,
) -> None:
    asked = 0

    async def password() -> str:
        nonlocal asked
        asked += 1
        return f"{TOKEN}-{asked}"

    async def run() -> list[str]:
        ledger = await PostgresLedger.open(location, min_size=0, max_size=2, password=password)
        try:
            return await _connections(ledger, 2)
        finally:
            await ledger.aclose()

    used = asyncio.run(run())
    assert asked == 3  # the migration connection, then one per pooled connection
    assert sorted(used) == [f"{TOKEN}-2", f"{TOKEN}-3"]
