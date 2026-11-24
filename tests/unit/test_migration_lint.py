"""Lint for new migrations (ADR-0018): shapes that lock a busy table, or break the previous release.

Migrations up to `GRANDFATHERED` predate these rules and are never edited (their checksums are
recorded in every ledger), so the rules apply from the next version on. Each rule is also run on
a bad and a good example, so a rule that stops matching fails here instead of passing silently.
"""

import re

import pytest

from spendgate.ledger import migrations
from spendgate.ledger.migrations import Migration

GRANDFATHERED = 4

_COMMENT = re.compile(r"--[^\n]*")
_CHECK = re.compile(r"\bADD\s+CONSTRAINT\s+(\w+)\s+CHECK\b[^;]*", re.IGNORECASE)
_VALIDATE = re.compile(r"\bVALIDATE\s+CONSTRAINT\s+(\w+)", re.IGNORECASE)
# Changes the previous release cannot run on: it reads, writes or relies on what they remove.
_DESTRUCTIVE = {
    "drops a table": r"\bDROP\s+TABLE\b",
    "drops a view": r"\bDROP\s+VIEW\b",
    "drops a column": r"\bDROP\s+COLUMN\b",
    "renames": r"\bRENAME\b",
    "changes a column type": r"\bALTER\s+COLUMN\s+\w+\s+(SET\s+DATA\s+)?TYPE\b",
    "adds NOT NULL to a column": r"\bSET\s+NOT\s+NULL\b",
    "deletes rows": r"\b(DELETE\s+FROM|TRUNCATE)\b",
    "adds a NOT NULL column without a default": (
        r"\bADD\s+COLUMN\s+(?:(?!DEFAULT)[^,;])*\bNOT\s+NULL\b(?:(?!DEFAULT)[^,;])*(,|;|$)"
    ),
}


def _code(sql: str) -> str:
    return _COMMENT.sub("", sql)


def lint(dialect: str, migration: Migration) -> list[str]:
    """What is wrong with one migration file; empty when it follows the rules."""
    code = _code(migration.sql)
    problems: list[str] = []
    if dialect == "postgres":
        validated = {m[1].lower() for m in _VALIDATE.finditer(code)}
        for added in _CHECK.finditer(code):
            name = added[1].lower()
            if not re.search(r"\bNOT\s+VALID\b", added[0], re.IGNORECASE):
                problems.append(f"CHECK {name} without NOT VALID: it scans the table locked")
            if name in validated:
                problems.append(f"CHECK {name} is validated in the same migration: use a later one")
    if migration.compatible:
        for what, pattern in _DESTRUCTIVE.items():
            if re.search(pattern, code, re.IGNORECASE):
                problems.append(f"marked compatible_with_previous but {what}")
    return problems


def _example(sql: str, *, compatible: bool = False) -> Migration:
    return Migration(GRANDFATHERED + 1, "example", sql, "x", compatible)


@pytest.mark.parametrize("dialect", ["postgres", "sqlite"])
def test_new_migrations_follow_the_migration_rules(dialect: str) -> None:
    found = {
        f"{m.version:04d}_{m.name}": lint(dialect, m)
        for m in migrations.load(dialect)
        if m.version > GRANDFATHERED
    }
    assert {name: problems for name, problems in found.items() if problems} == {}


def test_both_dialects_have_the_same_versions_and_markers() -> None:
    """Compatibility is a property of the release, not of one backend."""
    postgres = [(m.version, m.name, m.compatible) for m in migrations.load("postgres")]
    sqlite = [(m.version, m.name, m.compatible) for m in migrations.load("sqlite")]
    assert postgres == sqlite


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE calls ADD CONSTRAINT c_ok CHECK (n >= 0);",
        "ALTER TABLE calls ADD CONSTRAINT c_ok CHECK (n >= 0) NOT VALID;\n"
        "ALTER TABLE calls VALIDATE CONSTRAINT c_ok;",
    ],
    ids=["no-not-valid", "validated-in-the-same-file"],
)
def test_a_postgres_check_constraint_needs_not_valid_and_a_later_validate(sql: str) -> None:
    assert lint("postgres", _example(sql))


def test_a_not_valid_check_and_its_validate_in_a_later_migration_pass() -> None:
    add = "-- the scan comes later\nALTER TABLE calls ADD CONSTRAINT c_ok CHECK (n >= 0) NOT VALID;"
    assert lint("postgres", _example(add, compatible=True)) == []
    assert lint("postgres", _example("ALTER TABLE calls VALIDATE CONSTRAINT c_ok;")) == []


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE old_things;",
        "DROP VIEW v;",
        "ALTER TABLE calls DROP COLUMN note;",
        "ALTER TABLE calls RENAME COLUMN note TO remark;",
        "ALTER TABLE calls ALTER COLUMN note TYPE INTEGER;",
        "ALTER TABLE calls ALTER COLUMN note SET NOT NULL;",
        "DELETE FROM calls WHERE 1 = 1;",
        "TRUNCATE calls;",
        "ALTER TABLE calls ADD COLUMN note TEXT NOT NULL;",
    ],
)
def test_a_migration_marked_compatible_may_not_be_destructive(sql: str) -> None:
    marked = _example(f"-- compatible_with_previous: true\n{sql}", compatible=True)
    assert lint("sqlite", marked)
    assert lint("postgres", marked)
    assert lint("sqlite", _example(sql)) == []  # a contract step may do it


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE calls ADD COLUMN note TEXT;",
        "ALTER TABLE calls ADD COLUMN tries INTEGER NOT NULL DEFAULT 0;",
        "CREATE TABLE extra (id TEXT PRIMARY KEY);",
        "CREATE INDEX calls_note ON calls(note);",
        "-- we never DROP TABLE here\nCREATE TABLE extra2 (id TEXT);",
    ],
)
def test_expand_steps_pass_when_marked_compatible(sql: str) -> None:
    assert lint("postgres", _example(sql, compatible=True)) == []


def test_the_marker_is_read_from_the_file() -> None:
    assert migrations.compatible_marker("-- compatible_with_previous: true\nSELECT 1;")
    assert not migrations.compatible_marker("-- compatible_with_previous: false\nSELECT 1;")
    assert not migrations.compatible_marker("SELECT 1; -- compatible_with_previous: true")
    assert not any(m.compatible for m in migrations.load("postgres")[:GRANDFATHERED])
