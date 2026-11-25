"""Drive sans-IO flows on Postgres: one READ COMMITTED transaction per flow, retried on conflict.

The flows take their row locks themselves (`$LOCK` becomes `FOR UPDATE`, budgets in ascending id
order), so two holds that share budgets queue instead of deadlocking. Deadlocks can still arise
(an admin change, a future flow with another order), so the whole flow is re-created from its
factory and run again, at most `MAX_RETRIES` times, then the ledger is reported unavailable.

A lock timeout (55P03) or a cancelled statement (57014) is not retried: the session bounds exist to
answer quickly, and a budget row held that long will most likely still be held on a retry.

Round trips are what a hot budget pays for: its row stays locked while the gateway waits on the
network. So `BEGIN` goes out with the first statement, a `Batch` goes out in one round trip
(psycopg pipeline mode), and a flow's `last` batch goes out with the `COMMIT`. A pooled connection
is not checked before use (that was one more round trip per transaction): a connection found dead
before the COMMIT was sent has committed nothing, so the flow is re-run on another one, after the
pool's own `check()` has dropped any other dead idle connection.
"""

import asyncio
import contextlib
import random
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Final

import psycopg
from psycopg import AsyncConnection, errors
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from spendgate.ledger.port import ConflictError, LedgerUnavailable
from spendgate.ledger.sql import POSTGRES, Batch, Flow, Row, Stmt, Tx, end_of, render

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
# A report or retention batch may take longer than the 5 s per-statement default: raise it to 60 s
# for that one transaction. Only the default this package applied (source `session`) is raised;
# a statement_timeout the operator set (DSN, role, database, server) is kept as it is.
SLOW_STATEMENT_TIMEOUT: Final = "60s"
_ALLOW_SLOW: Final = (
    "SELECT set_config('statement_timeout', %(value)s, true) FROM pg_settings"
    " WHERE name = 'statement_timeout' AND source = 'session'"
)


class RetryStats:
    """How often flows were re-run, by SQLSTATE (40P01 deadlock, 40001 serialization, 23505, and
    08006 for a connection found dead before its COMMIT)."""

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


type _Sql = tuple[str, Mapping[str, object] | None]
_PIPELINE: Final = psycopg.Pipeline.is_supported()  # libpq 14 or later; else one at a time
_LOST: Final = "08006"


class ConnectionDropped(Exception):
    """The connection died before the COMMIT was sent: nothing committed, the flow may re-run."""


async def _rows(cursor: psycopg.AsyncCursor[Row]) -> list[Row]:
    return list(await cursor.fetchall()) if cursor.description is not None else []


async def _send(conn: Conn, statements: Sequence[_Sql]) -> list[list[Row]]:
    """Run the statements in order, in one round trip when there are several."""
    if len(statements) == 1 or not _PIPELINE:
        return [await _rows(await conn.execute(text, params)) for text, params in statements]
    async with conn.pipeline():  # leaving it syncs once: one round trip; the first error raises
        cursors = [await conn.execute(text, params) for text, params in statements]
    return [await _rows(c) for c in cursors]


def _sql(stmt: Stmt) -> _Sql:
    return render(stmt.sql, POSTGRES), stmt.params


async def _apply(conn: Conn, opening: list[_Sql], step: Stmt | Batch | Tx) -> list[Row]:
    """One step, after the still unsent `opening` (BEGIN) in the same round trip."""
    if isinstance(step, Tx):
        await _send(conn, [*opening, (_TX_SQL[step], None)])
        return []
    if isinstance(step, Stmt):
        return (await _send(conn, [*opening, _sql(step)]))[-1]
    results = await _send(conn, [*opening, *map(_sql, step.stmts)])
    step.results.extend(results[len(opening) :])
    return []


async def run_flow[T](conn: Conn, flow: Flow[T], *, slow: bool = False) -> T:
    """One attempt: BEGIN, step the flow to its end, COMMIT. Rolls back on any error."""
    opening: list[_Sql] = [(_TX_SQL[Tx.BEGIN], None)]
    if slow:
        opening.append((_ALLOW_SLOW, {"value": SLOW_STATEMENT_TIMEOUT}))
    committing = False
    try:
        try:
            step = next(flow)
            while not (isinstance(step, Batch) and step.last):
                committing = committing or step is Tx.COMMIT  # a flow may commit part of its work
                rows = await _apply(conn, opening, step)
                opening = []
                step = flow.send(rows)
        except StopIteration as done:
            result: T = done.value
        else:  # the last writes: the flow has finished before they and the COMMIT go out
            result = end_of(flow)
            committing = True
            await _send(conn, [*opening, *map(_sql, step.stmts), (_TX_SQL[Tx.COMMIT], None)])
            return result
        if not opening and conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            committing = True
            await conn.execute(_TX_SQL[Tx.COMMIT])
        return result
    except BaseException as exc:
        # A broken connection cannot roll back; the pool discards it, the error is reported.
        with contextlib.suppress(psycopg.Error):
            await asyncio.shield(conn.rollback())
        if isinstance(exc, psycopg.OperationalError) and conn.broken and not committing:
            raise ConnectionDropped(str(exc)) from exc
        raise
    finally:
        flow.close()


type WaitHook = Callable[[float], None]


class PoolUse:
    """Connections lent to a transaction right now. Counted here, not read from the pool's stats:
    those also count connections still being opened, which nobody is using yet."""

    def __init__(self) -> None:
        self.lent = 0


@contextlib.contextmanager
def _lent(use: PoolUse | None) -> Iterator[None]:
    if use is None:
        yield
        return
    use.lent += 1
    try:
        yield
    finally:
        use.lent -= 1


async def run_with_retry[T](  # noqa: PLR0913 - the metrics hooks are optional keywords
    pool: Pool,
    make_flow: Callable[[], Flow[T]],
    stats: RetryStats,
    *,
    slow: bool = False,
    on_wait: WaitHook | None = None,
    use: PoolUse | None = None,
) -> T:
    """`on_wait` gets how long each attempt queued for a connection, in ms; `use` counts the
    connections lent out (both are metrics)."""
    for attempt in range(MAX_RETRIES + 1):
        asked, connected = time.monotonic(), False  # a duration only; ledger time is the DB's
        try:
            async with pool.connection() as conn:
                connected = True
                _waited(on_wait, asked)
                with _lent(use):
                    return await run_flow(conn, make_flow(), slow=slow)
        except ConnectionDropped as exc:
            stats.by_sqlstate[_LOST] += 1
            if attempt == MAX_RETRIES:
                stats.exhausted += 1
                raise LedgerUnavailable(f"ledger connection lost: {exc}") from exc
            await pool.check()  # the pool's own health check: drop the other dead idle connections
        except psycopg.Error as exc:
            if not connected:  # no connection came (PoolTimeout): the whole wait was queueing
                _waited(on_wait, asked)
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


def _waited(on_wait: WaitHook | None, asked: float) -> None:
    if on_wait is not None:
        with contextlib.suppress(Exception):  # a metrics fault must not fail a money transaction
            on_wait((time.monotonic() - asked) * 1000)
