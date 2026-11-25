"""SQLite-specific behaviour: file mode, pragmas, migrations, audit triggers, failure mapping."""

import contextlib
import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from spendgate.core.budgets import Period, ScopeKind
from spendgate.ledger import migrations
from spendgate.ledger.port import CurrencyMismatch, LedgerUnavailable
from spendgate.ledger.sqlite import LedgerFileModeError, SqliteLedger
from tests.contract.rig import CURRENCY, T0, E, Rig


@pytest.fixture
def rig(tmp_path: Path, new_id) -> Iterator[Rig]:  # type: ignore[no-untyped-def]  # noqa: ANN001
    made = Rig("sqlite", str(tmp_path / "ledger.db"), new_id)
    yield made
    for conn in _raw_connections:
        conn.close()
    _raw_connections.clear()
    made.close()


_raw_connections: list[sqlite3.Connection] = []  # closed by the rig fixture's teardown


def raw(rig: Rig) -> sqlite3.Connection:
    conn = sqlite3.connect(rig.location)
    _raw_connections.append(conn)
    return conn


# --- T-LED-02: file mode ---------------------------------------------------------------------


def test_t_led_02_a_new_ledger_file_is_created_0600(tmp_path: Path) -> None:
    path = tmp_path / "new.db"
    os.umask(0o022)
    SqliteLedger.open(path, currency=CURRENCY).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o666, 0o400])
def test_t_led_02_an_existing_file_with_another_mode_is_refused(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "loose.db"
    SqliteLedger.open(path).close()
    path.chmod(mode)
    with pytest.raises(LedgerFileModeError, match="0600"):
        SqliteLedger.open(path)


def test_wal_and_integrity_pragmas_are_set(rig: Rig) -> None:
    conn = rig.ledger._conn  # type: ignore[attr-defined]
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# --- migrations ------------------------------------------------------------------------------


def test_migrations_are_recorded_once_and_reopening_is_idempotent(rig: Rig) -> None:
    rig.restart()
    rig.restart()
    rows = raw(rig).execute("SELECT version, name FROM schema_migrations").fetchall()
    assert [name for _, name in rows] == [
        "init",
        "usage",
        "scope_display_name",
        "calls_http_status_check",
    ]
    assert [version for version, _ in rows] == [1, 2, 3, 4]


def test_a_migration_edited_after_it_was_applied_is_refused(rig: Rig) -> None:
    rig.ledger.close()  # type: ignore[attr-defined]
    conn = raw(rig)
    conn.execute("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1")
    conn.commit()
    conn.close()
    with pytest.raises(LedgerUnavailable, match="changed after"):
        SqliteLedger.open(rig.location)


def test_a_database_from_a_newer_spendgate_is_refused(rig: Rig) -> None:
    rig.ledger.close()  # type: ignore[attr-defined]
    conn = raw(rig)
    conn.execute(
        "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
        "VALUES (99, 'future', 'x', 0)"
    )
    conn.commit()
    conn.close()
    with pytest.raises(LedgerUnavailable, match="newer"):
        SqliteLedger.open(rig.location)


def test_the_packaged_migrations_are_numbered_and_money_is_integer_only() -> None:
    packaged = migrations.load("sqlite")
    assert [m.version for m in packaged] == list(range(1, len(packaged) + 1))
    text = " ".join(m.sql for m in packaged).upper()
    for banned in ("REAL", "FLOAT", "DOUBLE", "NUMERIC", "DECIMAL"):
        assert not re.search(rf"\b{banned}\b", text), banned


def test_startup_never_mutates_a_reservation_row(rig: Rig) -> None:
    w = rig.world("20")
    rig.hold_ok(w, "A", "1")
    query = "SELECT * FROM reservations ORDER BY id"
    before = raw(rig).execute(query).fetchall()
    rig.restart()
    assert raw(rig).execute(query).fetchall() == before


# --- append-only audit -----------------------------------------------------------------------


def test_t_adm_05_admin_audit_is_append_only_and_admin_changes_write_it(rig: Rig) -> None:
    w = rig.world("10")
    rig.run(rig.ledger.update_budget(w.budget.id, actor="admin-1", now=T0, cap_nano=E("5")))
    rig.run(
        rig.ledger.create_grant(
            w.budget.id, amount_nano=E("1"), valid_from=T0, valid_until=T0 + 10,
            actor="admin-1", reason="r", now=T0,
        )
    )  # fmt: skip
    rig.run(rig.ledger.ack_overrun(w.budget.id, actor="admin-1", reason="ok", now=T0))
    conn = raw(rig)
    actions = [r[0] for r in conn.execute("SELECT action FROM admin_audit ORDER BY rowid")]
    assert actions == [
        "scope.create", "scope.create", "budget.create", "budget.set_cap", "budget.grant",
        "overrun.ack",
    ]  # fmt: skip
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE admin_audit SET actor_key_id = 'someone-else'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM admin_audit")


# --- failure mapping and constraints ---------------------------------------------------------


def test_m12_a_closed_store_raises_ledger_unavailable_so_callers_fail_closed(rig: Rig) -> None:
    w = rig.world("10")
    rig.ledger.close()  # type: ignore[attr-defined]
    with pytest.raises(LedgerUnavailable):
        rig.hold(w, "A", "1")


def test_the_schema_rejects_negative_money_and_dangling_keys(rig: Rig) -> None:
    w = rig.world("10")
    conn = raw(rig)
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO budget_periods (budget_id, period_start, spent_nano) VALUES (?, 0, -1)",
            (w.budget.id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reservations (id, state, owner_instance, lease_expires_at, key_id, "
            "scope_id, meta, created_at) VALUES ('x', 'pending', 'A', 1, 'no-key', ?, '{}', 1)",
            (w.scope.id,),
        )


def test_ids_are_time_ordered_lowercase_ulids() -> None:
    from spendgate.ledger.ids import new_id  # noqa: PLC0415

    ids = [new_id() for _ in range(50)]
    assert all(re.fullmatch(r"[0-9a-hjkmnp-tv-z]{26}", i) for i in ids)
    assert len(set(ids)) == 50


def test_scope_kinds_round_trip_through_the_store(rig: Rig) -> None:
    org = rig.run(rig.ledger.create_scope(ScopeKind.ORG, "acme", None, now=T0))
    project = rig.run(rig.ledger.create_scope(ScopeKind.PROJECT, "app", org.id, now=T0))
    assert project.path == "org:acme/project:app"
    assert rig.run(rig.ledger.get_scope(project.id)) == project
    assert rig.run(rig.ledger.get_scope("ghost")) is None
    budget = rig.run(rig.ledger.create_budget(project.id, Period.MONTH, E("3"), now=T0))
    assert budget.period is Period.MONTH


# --- loader and open errors ------------------------------------------------------------------


def test_the_migration_loader_refuses_odd_names_and_gaps(monkeypatch: pytest.MonkeyPatch) -> None:
    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

        def read_text(self, encoding: str) -> str:
            return "SELECT 1;"

    class Folder:
        def __init__(self, names: list[str]) -> None:
            self.names = names

        def joinpath(self, _: str) -> "Folder":
            return self

        def iterdir(self) -> list[Entry]:
            return [Entry(n) for n in self.names]

    def load_with(names: list[str]) -> list[migrations.Migration]:
        monkeypatch.setattr(migrations, "files", lambda _: Folder(names))
        return migrations.load("sqlite")

    assert [m.version for m in load_with([".gitkeep", "0001_a.sql", "0002_b.sql"])] == [1, 2]
    with pytest.raises(ValueError, match="NNNN_name"):
        load_with(["init.sql"])
    with pytest.raises(ValueError, match="without gaps"):
        load_with(["0001_a.sql", "0003_c.sql"])


def test_a_failing_migration_rolls_back_and_the_open_error_is_ledger_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = migrations.Migration(1, "broken", "CREATE TABLE t (a); NOT SQL;", "x")
    monkeypatch.setattr(migrations, "load", lambda _: [broken])
    path = tmp_path / "broken.db"
    with pytest.raises(LedgerUnavailable, match="cannot open"):
        SqliteLedger.open(path)
    with contextlib.closing(sqlite3.connect(path)) as check:
        assert check.execute("SELECT name FROM sqlite_master WHERE name = 't'").fetchall() == []


def test_an_unreadable_location_is_ledger_unavailable(tmp_path: Path) -> None:
    with pytest.raises((LedgerUnavailable, OSError)):
        SqliteLedger.open(tmp_path / "missing-dir" / "ledger.db")


def test_settle_needs_at_least_one_attempt(rig: Rig) -> None:
    w = rig.world("10")
    rig.hold_ok(w, "A", "1")
    with pytest.raises(ValueError, match="at least one attempt"):
        rig.run(rig.ledger.settle("A", 0, (), T0))


def test_the_ledger_currency_is_persisted_and_a_different_one_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "currency.db"
    SqliteLedger.open(path, currency="EUR").close()
    SqliteLedger.open(path, currency="EUR").close()  # same currency: fine
    with pytest.raises(CurrencyMismatch, match=r"EUR.*USD|USD.*EUR") as err:
        SqliteLedger.open(path, currency="USD")
    assert "EUR" in str(err.value) and "USD" in str(err.value)
