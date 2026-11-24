"""ADR-0018: migrating is an explicit step, and code may run on a schema one compatible step ahead.

Every test runs on both adapters (Postgres with `-m postgres`). An "older release" is simulated by
packaging fewer migrations (`migrations.load` patched); a "newer release" by packaging one more,
marked `compatible_with_previous`, or by writing its row the way that release would.
"""

import asyncio
import hashlib
import secrets
import sqlite3
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from psycopg import errors
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from spendgate.ledger import migrations
from spendgate.ledger.migrations import Migration, SchemaBehind
from spendgate.ledger.port import LedgerUnavailable
from spendgate.ledger.postgres import PostgresLedger
from spendgate.ledger.sqlite import SqliteLedger
from tests.contract.rig import CURRENCY, workdir_location

REAL_LOAD = migrations.load
EXPAND_SQL = "-- compatible_with_previous: true\nCREATE TABLE expand_probe (id INTEGER);\n"


def dialect(kind: str) -> str:
    return "sqlite" if kind == "sqlite" else "postgres"


def open_and_close(kind: str, location: str, *, migrate: bool) -> None:
    if kind == "sqlite":
        SqliteLedger.open(location, currency=CURRENCY, migrate=migrate).close()
    else:  # `open` makes no pool, so there is nothing to close
        asyncio.run(PostgresLedger.open(location, currency=CURRENCY, migrate=migrate))


def execute(
    kind: str, location: str, statement: str, params: tuple[object, ...] = ()
) -> list[tuple[object, ...]]:
    """Run one statement; `?` placeholders on both dialects."""
    if kind == "sqlite":
        conn = sqlite3.connect(location, isolation_level=None)
        try:
            return conn.execute(statement, params).fetchall()
        finally:
            conn.close()
    with psycopg.connect(location, autocommit=True) as pg:
        cursor = pg.execute(statement.replace("?", "%s").encode(), params)
        return cursor.fetchall() if cursor.description else []


def recorded(kind: str, location: str) -> list[tuple[int, bool]]:
    rows = execute(kind, location, "SELECT version, compatible FROM schema_migrations ORDER BY 1")
    return [(int(str(v)), bool(c)) for v, c in rows]


def has_table(kind: str, location: str, name: str) -> bool:
    query = (
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
        if kind == "sqlite"
        else "SELECT 1 FROM pg_tables WHERE schemaname = current_schema() AND tablename = ?"
    )
    return bool(execute(kind, location, query, (name,)))


def packaging(
    monkeypatch: pytest.MonkeyPatch, make: Callable[[list[Migration]], list[Migration]]
) -> None:
    """Make this process package `make(real migrations)` instead of what it ships."""
    monkeypatch.setattr(migrations, "load", lambda d: make(REAL_LOAD(d)))


def expand_step(packaged: list[Migration]) -> list[Migration]:
    checksum = hashlib.sha256(EXPAND_SQL.encode()).hexdigest()
    step = Migration(len(packaged) + 1, "expand_probe", EXPAND_SQL, checksum, compatible=True)
    return [*packaged, step]


@pytest.fixture
def location(adapter: str, tmp_path: Path) -> str:
    return workdir_location(adapter, tmp_path)


def test_without_migrate_a_schema_that_is_behind_is_refused_and_left_as_it_is(
    adapter: str, location: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = len(REAL_LOAD(dialect(adapter)))
    packaging(monkeypatch, lambda ms: ms[:-1])  # the previous release migrates the database
    open_and_close(adapter, location, migrate=True)
    monkeypatch.setattr(migrations, "load", REAL_LOAD)  # this release starts on it
    with pytest.raises(SchemaBehind, match=rf"version {latest - 1}.*needs {latest}.*migrate"):
        open_and_close(adapter, location, migrate=False)
    assert [v for v, _ in recorded(adapter, location)] == list(range(1, latest))
    open_and_close(adapter, location, migrate=True)  # `spendgate migrate`
    open_and_close(adapter, location, migrate=False)  # then `serve` starts
    assert [v for v, _ in recorded(adapter, location)] == list(range(1, latest + 1))


def test_without_migrate_an_empty_database_is_refused_and_nothing_is_created(
    adapter: str, location: str
) -> None:
    with pytest.raises(SchemaBehind, match="version 0"):
        open_and_close(adapter, location, migrate=False)
    assert not has_table(adapter, location, "schema_migrations")
    assert not has_table(adapter, location, "budgets")


def test_the_previous_release_starts_on_a_schema_one_compatible_step_ahead(
    adapter: str, location: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = len(REAL_LOAD(dialect(adapter)))
    packaging(monkeypatch, expand_step)  # the next release migrates, marking its step
    open_and_close(adapter, location, migrate=True)
    assert recorded(adapter, location)[-1] == (latest + 1, True)
    assert has_table(adapter, location, "expand_probe")
    monkeypatch.setattr(migrations, "load", REAL_LOAD)  # rolled back to this release
    open_and_close(adapter, location, migrate=False)
    open_and_close(adapter, location, migrate=True)  # nothing to apply, nothing refused
    assert recorded(adapter, location)[-1] == (latest + 1, True)


@pytest.mark.parametrize(
    ("ahead", "compatible"),
    [pytest.param(1, False, id="contract-step"), pytest.param(2, True, id="two-steps")],
)
def test_a_newer_schema_that_is_not_one_compatible_step_ahead_is_refused(
    adapter: str, location: str, ahead: int, compatible: bool
) -> None:
    open_and_close(adapter, location, migrate=True)
    latest = len(REAL_LOAD(dialect(adapter)))
    for version in range(latest + 1, latest + ahead + 1):
        execute(
            adapter,
            location,
            "INSERT INTO schema_migrations (version, name, checksum, applied_at, compatible) "
            "VALUES (?, 'future', 'x', 0, ?)",
            (version, compatible),
        )
    for migrate in (False, True):
        with pytest.raises(LedgerUnavailable, match=rf"newer spendgate.*{latest + ahead}"):
            open_and_close(adapter, location, migrate=migrate)


def test_a_bookkeeping_table_without_the_compatible_flag_is_read_and_then_upgraded(
    adapter: str, location: str
) -> None:
    """A ledger last migrated by a release before ADR-0018 has no `compatible` column."""
    open_and_close(adapter, location, migrate=True)
    for statement in (
        "CREATE TABLE old_migrations AS SELECT version, name, checksum, applied_at "
        "FROM schema_migrations",
        "DROP TABLE schema_migrations",
        "ALTER TABLE old_migrations RENAME TO schema_migrations",
    ):
        execute(adapter, location, statement)
    open_and_close(adapter, location, migrate=False)  # current versions: starts as it is
    open_and_close(adapter, location, migrate=True)  # adds the flag, false for every old row
    latest = len(REAL_LOAD(dialect(adapter)))
    assert recorded(adapter, location) == [(v, False) for v in range(1, latest + 1)]


@pytest.mark.postgres
def test_each_postgres_migration_commits_on_its_own(
    postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So a `NOT VALID` constraint is validated by a later migration without the first one's
    lock held across the scan; a failure leaves the schema at the last committed version."""
    location = workdir_location("postgres", tmp_path)
    broken = Migration(3, "broken", "SELECT * FROM no_such_table", "x")
    packaging(monkeypatch, lambda ms: [*ms[:2], broken])
    with pytest.raises(LedgerUnavailable, match="no_such_table"):
        open_and_close("postgres", location, migrate=True)
    assert recorded("postgres", location) == [(1, False), (2, False)]


@pytest.mark.postgres
def test_a_migrating_open_with_nothing_to_do_needs_no_table_ownership(
    postgres_url: str, tmp_path: Path
) -> None:
    """`serve --migrate` (or `migrate`) on a current schema changes nothing, so an app role that
    may only read and write the tables starts: no DDL, which needs the owner and a strong lock."""
    location = workdir_location("postgres", tmp_path)
    open_and_close("postgres", location, migrate=True)  # the owner migrates
    schema = str(execute("postgres", location, "SELECT current_schema()")[0][0])
    role = f"sg_app_{secrets.token_hex(4)}"
    try:
        execute("postgres", location, f'CREATE ROLE "{role}" NOLOGIN')
    except errors.InsufficientPrivilege:
        pytest.skip("the test database user may not create roles")
    try:
        execute("postgres", location, f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
        execute(
            "postgres",
            location,
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" TO "{role}"',
        )
        options = f"{conninfo_to_dict(location)['options']} -c role={role}"
        open_and_close("postgres", make_conninfo(location, options=options), migrate=True)
    finally:
        execute("postgres", location, f'DROP OWNED BY "{role}"')
        execute("postgres", location, f'DROP ROLE "{role}"')
