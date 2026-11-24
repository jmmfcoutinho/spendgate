"""Postgres ledger: many gateway hosts share one set of budgets.

An `AsyncConnectionPool` runs each flow in one READ COMMITTED transaction (see
`postgres_driver`). The pool is bound to an event loop, so it is created on first use inside
the running loop; `open` only migrates and checks the currency, on a throwaway connection, and
may therefore be called from any loop (or none: `asyncio.run(PostgresLedger.open(...))`).

The sweeper may run on every instance: each expiry is a state-guarded UPDATE (`WHERE state =
'pending'`), so a second sweeper finds nothing to do. No advisory lock is needed for
correctness; it would only save duplicate scans, so it is not used.

Every pooled connection is bounded (`SESSION_DEFAULTS`, `KEEPALIVES`) so that a locked row or a
silent server ends in `LedgerUnavailable` instead of holding a call; what the operator set wins.
"""

import asyncio
import time
from collections.abc import Callable
from typing import Final, Self

import psycopg
from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from spendgate.ledger import migrations
from spendgate.ledger.admin_flow import currency_flow
from spendgate.ledger.common import i
from spendgate.ledger.engine import LedgerEngine
from spendgate.ledger.ids import new_id as default_new_id
from spendgate.ledger.port import LedgerUnavailable
from spendgate.ledger.postgres_driver import Conn, Pool, RetryStats, run_flow, run_with_retry
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
# A dead server or a dropped network is noticed in about 30 s; a DSN that names any of these wins.
KEEPALIVES: Final = {
    "keepalives": "1",
    "keepalives_idle": "15",
    "keepalives_interval": "5",
    "keepalives_count": "3",
}
_SET_DEFAULTS = (
    "SELECT set_config(s.name, d.value, false) FROM pg_settings AS s"
    " JOIN unnest(%s::text[], %s::text[]) AS d(name, value) ON d.name = s.name"
    " WHERE s.source = 'default'"
)
# One migrator at a time per schema; the key is stable across processes (hashtext is).
_MIGRATE_LOCK = "SELECT pg_advisory_xact_lock(hashtext('spendgate-migrate:' || current_schema()))"


async def _migrate(conn: Conn, clock: Callable[[], int]) -> None:
    packaged = migrations.load("postgres")
    async with conn.transaction():
        await conn.execute(_MIGRATE_LOCK)
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at BIGINT NOT NULL)"
        )
        cursor = await conn.execute("SELECT version, checksum FROM schema_migrations")
        applied = {int(r["version"]): str(r["checksum"]) for r in await cursor.fetchall()}
        if set(applied) - {m.version for m in packaged}:
            raise LedgerUnavailable(
                "the ledger was migrated by a newer spendgate; refusing to start"
            )
        for migration in packaged:
            if migration.version in applied:
                if applied[migration.version] != migration.checksum:
                    raise LedgerUnavailable(
                        f"migration {migration.version} changed after it was applied"
                    )
                continue
            await conn.execute(migration.sql.encode())  # no parameters: several statements allowed
            await conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                "VALUES (%s, %s, %s, %s)",
                (migration.version, migration.name, migration.checksum, clock()),
            )


def keepalives_for(dsn: str) -> dict[str, str]:
    """The keepalive options the DSN does not set itself (libpq options it names are kept)."""
    named = conninfo_to_dict(dsn)
    return {k: v for k, v in KEEPALIVES.items() if k not in named}


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
    ) -> None:
        super().__init__(currency, new_id)
        self._dsn = dsn
        self._keepalives = keepalives_for(dsn)
        self._sizes = (min_size, max_size)
        self._pool_timeout = pool_timeout
        self._pool: Pool | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.stats = RetryStats()

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
    ) -> Self:
        """Migrate and check the ledger currency. Never touches a reservation."""
        try:
            # No session bounds here: a migration may wait for a peer's lock or run long.
            async with await AsyncConnection.connect(
                make_conninfo(dsn, **keepalives_for(dsn)),
                autocommit=True,
                row_factory=dict_row,
                connect_timeout=_CONNECT_TIMEOUT_S,
            ) as conn:
                await _migrate(conn, clock)
                await run_flow(conn, currency_flow(currency))
        except psycopg.Error as exc:
            raise LedgerUnavailable(f"cannot open ledger: {exc}") from exc
        return cls(
            dsn,
            currency,
            new_id,
            min_size=min_size,
            max_size=max_size,
            pool_timeout=pool_timeout,
        )

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The event loop the pool lives on (None until the first transaction)."""
        return self._loop

    async def db_now_ms(self) -> int:
        """The server's `clock_timestamp()`: what the skew warning compares the gateway against."""
        return await self._run(_now_flow, write=False)

    async def aclose(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    async def _ensure_pool(self) -> Pool:
        if self._pool is None:  # no await before the assignment: concurrent callers share it
            self._loop = asyncio.get_running_loop()
            low, high = self._sizes
            self._pool = AsyncConnectionPool(
                self._dsn,
                min_size=low,
                max_size=high,
                timeout=self._pool_timeout,
                open=False,
                kwargs={
                    "autocommit": True,  # transactions are explicit, as the flows ask for them
                    "row_factory": dict_row,
                    "connect_timeout": _CONNECT_TIMEOUT_S,
                    **self._keepalives,
                },
                configure=bound_session,
                check=AsyncConnectionPool.check_connection,
            )
        pool = self._pool
        await pool.open()  # idempotent
        return pool

    async def _run[T](self, make_flow: Callable[[], Flow[T]], *, write: bool = True) -> T:
        # `write` is moot here: reads still lock budget rows, so every flow gets the same shape.
        pool = await self._ensure_pool()
        return await run_with_retry(pool, make_flow, self.stats)
