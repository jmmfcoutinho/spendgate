"""Postgres login per connection: an IAM token source and a rotated DSN reach new connections.

The tests log in as a throwaway role whose password they set before each login, so every token
and rotated password is one the server really accepts, whether it checks passwords (CI) or trusts
loopback (a local server). What they check is that the pool asks for a password each time it
connects, and uses the DSN given last.
"""

import asyncio
import secrets
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from spendgate.ledger.port import LedgerUnavailable
from spendgate.ledger.postgres import PostgresLedger
from tests.contract.rig import workdir_location

pytestmark = pytest.mark.postgres

TOKEN = "fake-iam-token-0000"  # noqa: S105  # pragma: allowlist secret
OLD_PW, NEW_PW = "fake-old-pw", "fake-new-pw"  # pragma: allowlist secret
BAD_PW = "fake-bad-pw-0000"  # pragma: allowlist secret


class Role:
    """A login role for one test; the test sets its password, as an IAM service would."""

    def __init__(self, admin_url: str) -> None:
        self.name = f"sg_login_{secrets.token_hex(4)}"
        self._admin = admin_url

    def set_password(self, password: str) -> None:
        with psycopg.connect(self._admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(self.name), sql.Literal(password)
                )
            )


class CountingLogin:
    """A token source that hands out a new numbered token for each login, and makes it valid."""

    def __init__(self, role: Role) -> None:
        self.asked = self.invalidated = 0
        self._role = role

    async def password(self) -> str:
        self.asked += 1
        token = f"{TOKEN}-{self.asked}"
        await asyncio.to_thread(self._role.set_password, token)
        return token

    def invalidate(self) -> None:
        self.invalidated += 1


@pytest.fixture
def role(postgres_url: str) -> Iterator[Role]:
    made = Role(postgres_url)
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN SUPERUSER PASSWORD {}").format(
                sql.Identifier(made.name), sql.Literal(OLD_PW)
            )
        )
    try:
        yield made
    finally:
        with psycopg.connect(postgres_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(made.name)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(made.name)))


def _as(location: str, role: Role) -> str:
    """The test's DSN logged in as `role`, with no password of its own."""
    fields = {k: v for k, v in conninfo_to_dict(location).items() if k != "password"}
    fields["user"] = role.name
    return make_conninfo("", **fields)


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
    location: str, role: Role
) -> None:
    login = CountingLogin(role)
    dsn = _as(location, role)

    async def run() -> list[str]:
        ledger = await PostgresLedger.open(dsn, min_size=0, max_size=2, login=login)
        try:
            return await _connections(ledger, 2)
        finally:
            await ledger.aclose()

    used = asyncio.run(run())
    assert (
        login.asked == 3 and login.invalidated == 0
    )  # the migration connection, then one per pooled connection
    assert sorted(used) == [f"{TOKEN}-2", f"{TOKEN}-3"]


def test_a_rotated_dsn_reaches_the_next_connection_and_open_ones_keep_theirs(
    location: str, role: Role
) -> None:
    base = _as(location, role)

    async def run() -> tuple[list[str], list[str]]:
        old = make_conninfo(base, password=OLD_PW)
        ledger = await PostgresLedger.open(old, min_size=0, max_size=2)
        try:
            pool = await ledger._ensure_pool()
            kept = await pool.getconn()
            await asyncio.to_thread(role.set_password, NEW_PW)  # the secret was rotated
            assert ledger.rotate_dsn(make_conninfo(base, password=NEW_PW))
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
    assert "password" not in conninfo_to_dict(base)


def test_a_refused_connection_names_no_option_or_password_value(location: str) -> None:
    dsn = make_conninfo(location, password=BAD_PW, sslmode="bogus-mode-0000")

    with pytest.raises(LedgerUnavailable) as info:
        asyncio.run(PostgresLedger.open(dsn))
    text = str(info.value)
    assert "cannot connect to the ledger database" in text
    assert "bogus-mode" not in text and BAD_PW not in text
    assert info.value.__cause__ is None and info.value.__context__ is None
