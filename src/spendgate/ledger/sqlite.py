"""SQLite ledger: one writer at a time, WAL, `BEGIN IMMEDIATE` for every money transaction.

Threading: one connection guarded by a lock, used from `asyncio.to_thread`. SQLite allows a
single writer anyway, so a lock in front of one connection costs nothing and keeps writers in
this process from fighting over the file lock (busy-wait polling is not fair); other processes
are serialised by `BEGIN IMMEDIATE` plus `busy_timeout`. A per-thread connection pool would
only add connections that wait on each other.
"""

import asyncio
import contextlib
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Literal, Self

from spendgate.ledger import migrations
from spendgate.ledger.admin_flow import currency_flow
from spendgate.ledger.engine import LedgerEngine
from spendgate.ledger.ids import new_id as default_new_id
from spendgate.ledger.port import LedgerError, LedgerUnavailable
from spendgate.ledger.sql import SQLITE, Flow, Row, Stmt, Tx, render

_MODE = 0o600


class LedgerFileModeError(LedgerUnavailable):
    """The ledger file is readable or writable by someone else (T-LED-02): refuse to start."""


def _prepare_file(path: Path) -> None:
    """Create the file 0600 or refuse an existing one that is not 0600 and ours."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _MODE)
    except FileExistsError:
        info = path.stat()
        if stat.S_IMODE(info.st_mode) != _MODE or info.st_uid != os.geteuid():
            raise LedgerFileModeError(
                f"{path} must be mode 0600 and owned by this user, found "
                f"{stat.S_IMODE(info.st_mode):04o}"
            ) from None
    else:
        os.fchmod(fd, _MODE)  # the umask may have taken bits away
        os.close(fd)


def _enable_wal(conn: sqlite3.Connection, patience_s: float) -> None:
    """Switch to WAL, retrying while another process holds the file.

    Changing the journal mode takes a lock that `busy_timeout` does not wait for, so several
    processes opening one brand-new file at once would otherwise fail here. WAL persists in
    the file, so only first openers ever contend.
    """
    deadline = time.monotonic() + patience_s
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _statements(script: str) -> list[str]:
    """Split a migration into statements (triggers and quoted `;` stay whole)."""
    found: list[str] = []
    pending = ""
    for piece in script.split(";"):
        pending += piece + ";"
        if sqlite3.complete_statement(pending):
            if pending.strip(" \n;"):
                found.append(pending)
            pending = ""
    if pending.strip(" \n;"):
        raise LedgerUnavailable("a migration ends inside a statement")
    return found


def _migrate(conn: sqlite3.Connection, clock: Callable[[], int]) -> None:
    """Apply pending migrations in one write transaction.

    The applied-version check happens after `BEGIN IMMEDIATE`, so a second process opening the
    same new file waits for the first, then finds everything applied and does nothing.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at BIGINT NOT NULL)"
        )
        applied = {
            r[0]: r[1] for r in conn.execute("SELECT version, checksum FROM schema_migrations")
        }
        packaged = migrations.load("sqlite")
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
            for statement in _statements(migration.sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, clock()),
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


class SqliteLedger(LedgerEngine):
    def __init__(self, conn: sqlite3.Connection, currency: str, new_id: Callable[[], str]) -> None:
        super().__init__(currency, new_id)
        self._conn = conn
        self._lock = threading.Lock()

    @classmethod
    def open(  # noqa: PLR0913 - connection tuning knobs
        cls,
        path: str | Path,
        *,
        currency: str = "USD",
        new_id: Callable[[], str] = default_new_id,
        synchronous: Literal["FULL", "NORMAL", "OFF"] = "FULL",
        busy_timeout_ms: int = 30_000,
        clock: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> Self:
        """Open (creating it 0600) and migrate. Never touches a reservation."""
        file = Path(path)
        _prepare_file(file)
        try:
            conn = sqlite3.connect(
                file, isolation_level=None, check_same_thread=False, timeout=busy_timeout_ms / 1000
            )
        except sqlite3.Error as exc:
            raise LedgerUnavailable(f"cannot open ledger {file}: {exc}") from exc
        try:
            conn.row_factory = sqlite3.Row
            _enable_wal(conn, busy_timeout_ms / 1000)
            conn.execute(f"PRAGMA synchronous = {synchronous}")
            conn.execute("PRAGMA foreign_keys = ON")
            _migrate(conn, clock)
        except (sqlite3.Error, LedgerUnavailable) as exc:
            conn.close()
            if isinstance(exc, LedgerUnavailable):
                raise
            raise LedgerUnavailable(f"cannot open ledger {file}: {exc}") from exc
        ledger = cls(conn, currency, new_id)
        try:
            ledger._drive(partial(currency_flow, currency), True)
        except LedgerError:
            conn.close()
            raise
        return ledger

    async def db_now_ms(self) -> int:
        """SQLite has no clock of its own: the file lives on this host, so local time is the
        database time and the skew warning cannot fire (T-CLK-03 matters for Postgres)."""
        return time.time_ns() // 1_000_000

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    async def _run[T](self, make_flow: Callable[[], Flow[T]], *, write: bool = True) -> T:
        return await asyncio.to_thread(self._drive, make_flow, write)

    def _drive[T](self, make_flow: Callable[[], Flow[T]], write: bool) -> T:
        with self._lock:
            flow = make_flow()
            try:
                self._conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                result = self._step_through(flow)
                if self._conn.in_transaction:
                    self._conn.execute("COMMIT")
                return result
            except sqlite3.Error as exc:
                self._rollback()
                raise LedgerUnavailable(f"ledger error: {exc}") from exc
            except BaseException:
                self._rollback()
                raise
            finally:
                flow.close()

    def _rollback(self) -> None:
        with contextlib.suppress(sqlite3.Error):  # connection gone: the original error is reported
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")

    def _step_through[T](self, flow: Flow[T]) -> T:
        try:
            step = next(flow)
            while True:
                step = flow.send(self._apply(step))
        except StopIteration as done:
            result: T = done.value
            return result

    def _apply(self, step: Stmt | Tx) -> list[Row]:
        if isinstance(step, Tx):
            self._conn.execute("BEGIN IMMEDIATE" if step is Tx.BEGIN else step.value.upper())
            return []
        cursor = self._conn.execute(render(step.sql, SQLITE), step.params)
        return [dict(row) for row in cursor.fetchall()]
