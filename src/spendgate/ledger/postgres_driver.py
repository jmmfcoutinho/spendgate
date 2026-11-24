"""Drive sans-IO flows on Postgres: one READ COMMITTED transaction per flow, retried on conflict.

The flows take their row locks themselves (`$LOCK` becomes `FOR UPDATE`, budgets in ascending id
order), so two holds that share budgets queue instead of deadlocking. Deadlocks can still arise
(an admin change, a future flow with another order), so the whole flow is re-created from its
factory and run again, at most `MAX_RETRIES` times, then the ledger is reported unavailable.

A lock timeout (55P03) or a cancelled statement (57014) is not retried: the session bounds exist to
answer quickly, and a budget row held that long will most likely still be held on a retry.
"""

import asyncio
import contextlib
import random
from collections import Counter
from collections.abc import Callable
from typing import Final

import psycopg
from psycopg import AsyncConnection, errors
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from spendgate.ledger.port import ConflictError, LedgerUnavailable
from spendgate.ledger.sql import POSTGRES, Flow, Row, Stmt, Tx, render

type Conn = AsyncConnection[DictRow]
type Pool = AsyncConnectionPool[Conn]

MAX_RETRIES: Final = 3
_BACKOFF_S: Final = 0.02  # jitter ceiling for the first retry; doubles each time
_TX_SQL: Final = {
    Tx.BEGIN: "BEGIN ISOLATION LEVEL READ COMMITTED",
    Tx.COMMIT: "COMMIT",
    Tx.ROLLBACK: "ROLLBACK",
}
_RESERVATIONS_KEY: Final = "reservations_pkey"
_RETRYABLE: Final = (errors.DeadlockDetected, errors.SerializationFailure)


class RetryStats:
    """How often flows were re-run, by SQLSTATE (40P01 deadlock, 40001 serialization, 23505)."""

    def __init__(self) -> None:
        self.by_sqlstate: Counter[str] = Counter()
        self.exhausted = 0

    @property
    def total(self) -> int:
        return sum(self.by_sqlstate.values())


def _retryable(exc: psycopg.Error) -> bool:
    """Conflicts that a fresh transaction resolves.

    A unique violation on any table but `reservations` is a lost race to create the same
    scope or session; the retry finds the row the winner committed.
    """
    return isinstance(exc, _RETRYABLE) or (
        isinstance(exc, errors.UniqueViolation) and exc.diag.constraint_name != _RESERVATIONS_KEY
    )


async def _apply(conn: Conn, step: Stmt | Tx) -> list[Row]:
    if isinstance(step, Tx):
        await conn.execute(_TX_SQL[step])
        return []
    cursor = await conn.execute(render(step.sql, POSTGRES), step.params)
    return list(await cursor.fetchall()) if cursor.description is not None else []


async def run_flow[T](conn: Conn, flow: Flow[T]) -> T:
    """One attempt: BEGIN, step the flow to its end, COMMIT. Rolls back on any error."""
    try:
        await conn.execute(_TX_SQL[Tx.BEGIN])
        try:
            step = next(flow)
            while True:
                step = flow.send(await _apply(conn, step))
        except StopIteration as done:
            result: T = done.value
        if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            await conn.execute(_TX_SQL[Tx.COMMIT])
        return result
    except BaseException:
        # A broken connection cannot roll back; the pool discards it, the error is reported.
        with contextlib.suppress(psycopg.Error):
            await asyncio.shield(conn.rollback())
        raise
    finally:
        flow.close()


async def run_with_retry[T](pool: Pool, make_flow: Callable[[], Flow[T]], stats: RetryStats) -> T:
    for attempt in range(MAX_RETRIES + 1):
        try:
            async with pool.connection() as conn:
                return await run_flow(conn, make_flow())
        except psycopg.Error as exc:
            if isinstance(exc, errors.UniqueViolation) and not _retryable(exc):
                raise ConflictError("reservation already exists") from exc
            if not _retryable(exc):
                raise LedgerUnavailable(f"ledger error: {exc}") from exc
            stats.by_sqlstate[exc.sqlstate or "?"] += 1
            if attempt == MAX_RETRIES:
                stats.exhausted += 1
                raise LedgerUnavailable(
                    f"ledger transaction conflicted {MAX_RETRIES + 1} times: {exc}"
                ) from exc
            # Jitter so the two sides of a deadlock do not collide again in lockstep.
            await asyncio.sleep(random.uniform(0, _BACKOFF_S * 2**attempt))  # noqa: S311
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises
