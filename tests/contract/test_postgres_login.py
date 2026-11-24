"""Postgres login per connection: an IAM token source and a rotated DSN reach new connections.

The local test server trusts loopback, so a password is accepted whatever it is; what these tests
check is that the pool asks for one each time it connects, and uses the DSN given last.
"""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from spendgate.ledger.postgres import PostgresLedger
from tests.contract.rig import workdir_location

pytestmark = pytest.mark.postgres

TOKEN = "fake-iam-token-0000"  # noqa: S105  # pragma: allowlist secret
OLD_PW, NEW_PW = "fake-old-pw", "fake-new-pw"  # pragma: allowlist secret


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


def test_a_rotated_dsn_reaches_the_next_connection_and_open_ones_keep_theirs(
    location: str,
) -> None:
    async def run() -> tuple[list[str], list[str]]:
        old = make_conninfo(location, password=OLD_PW)
        ledger = await PostgresLedger.open(old, min_size=0, max_size=2)
        try:
            pool = await ledger._ensure_pool()
            kept = await pool.getconn()
            assert ledger.rotate_dsn(make_conninfo(location, password=NEW_PW))
            fresh = await pool.getconn()
            before, after = [kept.info.password or ""], [fresh.info.password or ""]
            await pool.putconn(kept)
            await pool.putconn(fresh)
            await ledger.db_now_ms()  # the ledger still works after the rotation
            return before, after
        finally:
            await ledger.aclose()

    before, after = asyncio.run(run())
    assert before == [OLD_PW] and after == [NEW_PW]
    assert "password" not in conninfo_to_dict(location)
