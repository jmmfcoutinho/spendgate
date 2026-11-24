"""Postgres ledger: many gateway hosts share one set of budgets.

An `AsyncConnectionPool` runs each flow in one READ COMMITTED transaction (see
`postgres_driver`). The pool is bound to an event loop, so it is created on first use inside
the running loop; `open` only migrates (or checks the schema version) and checks the currency, on
a throwaway connection, and may therefore be called from any loop (or none:
`asyncio.run(PostgresLedger.open(...))`).

The sweeper may run on every instance: each expiry is a state-guarded UPDATE (`WHERE state =
'pending'`), so a second sweeper finds nothing to do. No advisory lock is needed for
correctness; it would only save duplicate scans, so it is not used.

Every pooled connection is bounded (`SESSION_DEFAULTS`, `TCP_DEFAULTS`) so that a locked row or a
silent server ends in `LedgerUnavailable` instead of holding a call; what the operator set wins.

The pool reads the DSN and the password each time it opens a connection, never once at start:
`login` (an IAM token source, `postgres_auth`) is asked per connection and forgets its token when a
connect fails, and `rotate_dsn` hands new connections a re-read DSN whose password changed. Open
connections keep their login.
"""

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Final, Self

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from spendgate.ledger import migrations
from spendgate.ledger.admin_flow import currency_flow
from spendgate.ledger.common import i
from spendgate.ledger.engine import LedgerEngine
from spendgate.ledger.ids import new_id as default_new_id
from spendgate.ledger.port import LedgerUnavailable, PoolStats
from spendgate.ledger.postgres_auth import Login, connection_class
from spendgate.ledger.postgres_driver import (
    Conn,
    ConnectionDropped,
    Pool,
    PoolUse,
    RetryStats,
    WaitHook,
    run_flow,
    run_with_retry,
)
from spendgate.ledger.sql import Flow, q

_CONNECT_TIMEOUT_S = 10
# Server-side bounds for each pooled connection. lock_timeout stays above deadlock_timeout (1 s by
# default) so a deadlock is still reported as one and retried; a lock or statement timeout is not
# retried (postgres_driver). Set only where the server reports the built-in default, so a value from
# the DSN `options`, PGOPTIONS, ALTER ROLE/DATABASE or postgresql.conf wins.
SESSION_DEFAULTS: Final = {
    "lock_timeout": "2s",
    "statement_timeout": "5s",
    "idle_in_transaction_session_timeout": "10s",
}
# libpq TCP options; a DSN that names any of them wins. Keepalives notice a dead server or a
# dropped network on an idle connection in about 30 s; tcp_user_timeout (ms) gives up on data the
# server never acknowledges (a COMMIT to a silent host) after 30 s, not ~15 min of retransmission.
TCP_DEFAULTS: Final = {
    "keepalives": "1",
    "keepalives_idle": "15",
    "keepalives_interval": "5",
    "keepalives_count": "3",
    "tcp_user_timeout": "30000",
}
_SET_DEFAULTS = (
    "SELECT set_config(s.name, d.value, false) FROM pg_settings AS s"
    " JOIN unnest(%s::text[], %s::text[]) AS d(name, value) ON d.name = s.name"
    " WHERE s.source = 'default'"
)
# One migrator at a time per schema; the key is stable across processes (hashtext is). Each
# step takes it for its transaction; a migrator also holds it for its whole run (session level),
# so an opener that only checks waits for the last step, not just the next commit.
_MIGRATE_KEY = "hashtext('spendgate-migrate:' || current_schema())"
_MIGRATE_LOCK = f"SELECT pg_advisory_xact_lock({_MIGRATE_KEY})"
_RUN_LOCK = f"SELECT pg_advisory_lock({_MIGRATE_KEY})"
_RUN_UNLOCK = f"SELECT pg_advisory_unlock({_MIGRATE_KEY})"


_CREATE_BOOKKEEPING = (
    "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
    "name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at BIGINT NOT NULL, "
    "compatible BOOLEAN NOT NULL DEFAULT false)"
)
# A table created before ADR-0018 lacks the flag; its rows are all contract steps.
_ADD_FLAG = "ALTER TABLE schema_migrations ADD COLUMN compatible BOOLEAN NOT NULL DEFAULT false"
_BOOKKEEPING_STATE = (
    "SELECT to_regclass('schema_migrations') IS NOT NULL AS found, EXISTS (SELECT 1 FROM "
    "pg_attribute WHERE attrelid = to_regclass('schema_migrations') AND attname = 'compatible' "
    "AND NOT attisdropped) AS flagged"
)


async def _ensure_bookkeeping(conn: Conn) -> None:
    """Create `schema_migrations` or add its flag, only when missing: even with IF NOT EXISTS,
    CREATE TABLE needs CREATE on the schema and ALTER TABLE ownership plus an ACCESS EXCLUSIVE
    lock, so an app role that only reads and writes could not start on a current schema."""
    cursor = await conn.execute(_BOOKKEEPING_STATE)
    row = await cursor.fetchone()
    if row is None or not row["found"]:
        await conn.execute(_CREATE_BOOKKEEPING)
    elif not row["flagged"]:
        await conn.execute(_ADD_FLAG)


async def _applied(conn: Conn) -> dict[int, migrations.Applied]:
    """The recorded migrations; none when the table does not exist yet (a new database)."""
    cursor = await conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL AS found")
    row = await cursor.fetchone()
    if row is None or not row["found"]:
        return {}
    cursor = await conn.execute("SELECT * FROM schema_migrations")
    return migrations.recorded(await cursor.fetchall())


async def _migrate(conn: Conn, clock: Callable[[], int], *, apply: bool) -> None:
    """Apply pending migrations, each in its own transaction, or (`apply` False) only check.

    One commit per migration, so a constraint added `NOT VALID` by one migration is validated by
    a later one without the strong lock of the first held across the scan (ADR-0018). Each step
    takes the migration lock and re-reads what is applied, so concurrent openers apply each
    version once; a failure leaves the schema at the last version that committed. While a run
    applies, it also holds the lock between steps: a replica that starts meanwhile would
    otherwise get it after the first commit and refuse a half-migrated schema.
    """
    if not apply:
        await _steps(conn, clock, apply=False)
        return
    await conn.execute(_RUN_LOCK)  # autocommit: held until the unlock or the connection closes
    try:
        await _steps(conn, clock, apply=True)
    finally:
        with contextlib.suppress(psycopg.Error):  # a broken connection has released it already
            await conn.execute(_RUN_UNLOCK)


async def _steps(conn: Conn, clock: Callable[[], int], *, apply: bool) -> None:
    packaged = migrations.load("postgres")
    while True:
        async with conn.transaction():
            await conn.execute(_MIGRATE_LOCK)  # also waits out a migrator that is running now
            applied = await _applied(conn)
            todo = migrations.pending_migrations(applied, packaged)
            if not apply:
                migrations.require_current(todo, applied)
                return
            await _ensure_bookkeeping(conn)  # a no-op on a current, flagged table
            if not todo:
                return
            migration = todo[0]
            await conn.execute(migration.sql.encode())  # no parameters: several statements allowed
            await conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at, compatible) "
                "VALUES (%s, %s, %s, %s, %s)",
                (migration.version, migration.name, migration.checksum, clock(),
                 migration.compatible),
            )  # fmt: skip


def same_but_password(old: str, new: str) -> bool:
    """Whether two DSNs differ in their password at most (the only change a reload may make)."""
    try:
        a, b = conninfo_to_dict(old), conninfo_to_dict(new)
    except psycopg.Error:  # its message may quote the DSN, password included
        a = b = {}
    if not a or not b:  # raised outside the handler: no context that quotes the DSN
        raise ValueError("not a valid connection string")
    a.pop("password", None)
    b.pop("password", None)
    return a == b


async def _connect_once(dsn: str, login: Login | None) -> Conn:
    """The migration connection, TCP options only: a migration may wait for a peer's lock.

    Every refusal is fixed text raised outside its handler: psycopg's and libpq's messages quote
    parts of the connection string, so neither they nor a chained exception may reach a log.
    """
    failed = ""
    try:
        tcp = tcp_options_for(dsn)
    except psycopg.Error as exc:
        failed = f"ledger.backend.dsn is not a valid connection string ({type(exc).__name__})"
    if failed:
        raise LedgerUnavailable(f"cannot open ledger: {failed}")
    token = {"password": await login.password()} if login is not None else {}
    try:
        return await connection_class(login).connect(
            make_conninfo(dsn, **tcp, **token),
            autocommit=True, row_factory=dict_row, connect_timeout=_CONNECT_TIMEOUT_S,
        )  # fmt: skip
    except psycopg.Error as exc:  # already `connect_failure` text from the connection class
        failed = str(exc)
    raise LedgerUnavailable(f"cannot open ledger: {failed}")


def tcp_options_for(dsn: str) -> dict[str, str]:
    """The `TCP_DEFAULTS` the DSN does not set itself (libpq options it names are kept)."""
    named = conninfo_to_dict(dsn)
    return {k: v for k, v in TCP_DEFAULTS.items() if k not in named}


async def bound_session(conn: Conn) -> None:
    """Pool `configure` hook: apply `SESSION_DEFAULTS` wherever nothing else set a value."""
    await conn.execute(_SET_DEFAULTS, (list(SESSION_DEFAULTS), list(SESSION_DEFAULTS.values())))


def _now_flow() -> Flow[int]:
    rows = yield q("SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000 AS BIGINT) AS ms")
    return i(rows[0], "ms")


class PostgresLedger(LedgerEngine):
    def __init__(  # noqa: PLR0913 - pool tuning knobs
        self,
        dsn: str,
        currency: str,
        new_id: Callable[[], str],
        *,
        min_size: int = 1,
        max_size: int = 10,
        pool_timeout: float = 10.0,
        login: Login | None = None,
    ) -> None:
        super().__init__(currency, new_id)
        self._dsn = dsn
        self._login = login
        self._tcp = tcp_options_for(dsn)
        self._sizes = (min_size, max_size)
        self._pool_timeout = pool_timeout
        self._pool: Pool | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.stats = RetryStats()
        # Set by the gateway to feed the pool wait histogram; None: not measured.
        self.pool_wait: WaitHook | None = None
        self._use = PoolUse()

    @classmethod
    async def open(  # noqa: PLR0913 - connection tuning knobs
        cls,
        dsn: str,
        *,
        currency: str = "USD",
        new_id: Callable[[], str] = default_new_id,
        clock: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        min_size: int = 1,
        max_size: int = 10,
        pool_timeout: float = 10.0,
        migrate: bool = True,
        login: Login | None = None,
    ) -> Self:
        """Migrate (or with `migrate` False refuse a schema that is behind, `SchemaBehind`) and
        check the ledger currency. Never touches a reservation."""
        conn = await _connect_once(dsn, login)
        try:
            async with conn:
                await _migrate(conn, clock, apply=migrate)
                await run_flow(conn, currency_flow(currency))
        except (psycopg.Error, ConnectionDropped) as exc:  # server messages: no connection string
            raise LedgerUnavailable(f"cannot open ledger: {exc}") from exc
        return cls(
            dsn,
            currency,
            new_id,
            min_size=min_size,
            max_size=max_size,
            pool_timeout=pool_timeout,
            login=login,
        )

    def rotate_dsn(self, dsn: str) -> bool:
        """Use a re-read DSN for every connection opened from now on; True when it is new.

        Raises ValueError unless only the password differs: a new host, database or user is a
        restart, never a silent move to another ledger while holds are open.
        """
        if not same_but_password(self._dsn, dsn):
            raise ValueError("the DSN changed more than its password; restart to apply it")
        changed, self._dsn = dsn != self._dsn, dsn
        return changed

    async def _connect_kwargs(self) -> dict[str, object]:
        """Pool `kwargs` hook, called for each new connection: the login password is current."""
        kwargs: dict[str, object] = {
            "autocommit": True,  # transactions are explicit, as the flows ask for them
            "row_factory": dict_row,
            "connect_timeout": _CONNECT_TIMEOUT_S,
            **self._tcp,
        }
        if self._login is not None:
            kwargs["password"] = await self._login.password()
        return kwargs

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The event loop the pool lives on (None until the first transaction)."""
        return self._loop

    async def db_now_ms(self) -> int:
        """The server's `clock_timestamp()`: what the skew warning compares the gateway against."""
        return await self._run(_now_flow, write=False)

    def pool_stats(self) -> PoolStats | None:
        """The pool's size and connections in use; None before the first transaction opens it."""
        if self._pool is None:
            return None
        size = self._pool.get_stats().get("pool_size", 0)  # idle, lent out or being opened
        return PoolStats(size, self._use.lent, self._sizes[1])

    async def aclose(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    async def _ensure_pool(self) -> Pool:
        if self._pool is None:  # no await before the assignment: concurrent callers share it
            self._loop = asyncio.get_running_loop()
            low, high = self._sizes
            self._pool = AsyncConnectionPool(
                lambda: self._dsn,  # read per connection: `rotate_dsn` reaches the next one
                min_size=low,
                max_size=high,
                timeout=self._pool_timeout,
                open=False,
                kwargs=self._connect_kwargs,
                connection_class=connection_class(self._login),
                configure=bound_session,  # no `check`: see postgres_driver (ConnectionDropped)
            )
        pool = self._pool
        await pool.open()  # idempotent
        return pool

    async def _run[T](
        self, make_flow: Callable[[], Flow[T]], *, write: bool = True, slow: bool = False
    ) -> T:
        # `write` is moot here: READ COMMITTED for every flow; the flows take their own locks.
        pool = await self._ensure_pool()
        return await run_with_retry(
            pool, make_flow, self.stats, slow=slow, on_wait=self.pool_wait, use=self._use
        )
