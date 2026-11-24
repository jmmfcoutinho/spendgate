"""Lint for new migrations (ADR-0018): shapes that lock a busy table, or break the previous release.

Migrations up to `GRANDFATHERED` predate these rules and are never edited (their checksums are
recorded in every ledger), so the rules apply from the next version on. Each rule is also run on
bad and good examples, so a rule that stops matching fails here instead of passing silently.

The patterns read SQL loosely and err towards a finding: a false alarm costs a reworded
migration, a miss costs a rollback that fails mid-call.
"""

import re

import pytest

from spendgate.ledger import migrations
from spendgate.ledger.migrations import Migration

GRANDFATHERED = 4

_I = re.IGNORECASE
# Read left to right, so that each hides the others' markers: a quoted name (kept), a string
# literal, a dollar-quoted body, a block or a line comment.
_LEXEME = re.compile(
    r"\"[^\"]*\"|'(?:[^']|'')*'|\$(\w*)\$(.*?)\$\1\$|/\*.*?\*/|--[^\n]*", re.DOTALL
)
_NAME = r"(?:\"[^\"]+\"|\w+)"  # a quoted name may hold spaces
# `ALTER TABLE [IF EXISTS] [ONLY] [schema.]name`; what follows is a comma-separated list of actions.
_ALTER_TABLE = re.compile(
    rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(?:{_NAME}\s*\.\s*)*{_NAME}\s*\*?\s*", _I
)
# An action that adds a column (COLUMN is optional) rather than a table constraint.
_ADD_COLUMN = re.compile(
    r"ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?!(?:CONSTRAINT|CHECK|UNIQUE|PRIMARY|FOREIGN|EXCLUDE)\b)\"?\w+",
    _I,
)
_NAMED_CHECK = re.compile(r"ADD\s+CONSTRAINT\s+\"?(\w+)\"?\s+CHECK\b", _I)
_NOT_VALID = re.compile(r"\bNOT\s+VALID\b", _I)
_VALIDATE = re.compile(r"\bVALIDATE\s+CONSTRAINT\s+\"?(\w+)", _I)
# Anywhere in a migration marked compatible: the previous release reads, writes or relies on what
# these remove, or writes rows that what they add rejects (it fails mid-call after a rollback).
_NOT_COMPATIBLE = {
    # Whatever it drops (a table, view, column, default, type, function, trigger, ...), the
    # previous release may use it. Dropping a constraint or an index, or NOT NULL, only loosens.
    "drops something": r"\bDROP\s+(?!CONSTRAINT\b|NOT\s+NULL\b|INDEX\b)",
    "renames": r"\bRENAME\b",
    "changes a column type": r"\bALTER\s+(?:COLUMN\s+)?\"?\w+\"?\s+(?:SET\s+DATA\s+)?TYPE\b",
    "adds NOT NULL to a column": r"\bSET\s+NOT\s+NULL\b",
    "deletes rows": r"\b(?:DELETE\s+FROM|TRUNCATE)\b",
    "creates a unique index": r"\bCREATE\s+UNIQUE\s+INDEX\b",
    "adds a foreign key": r"\bREFERENCES\b",
}
# Per ALTER TABLE action, matched at its start.
_NOT_COMPATIBLE_ACTION = {
    "adds a constraint": r"ADD\s+(?:CONSTRAINT|CHECK|UNIQUE|PRIMARY\s+KEY|FOREIGN\s+KEY|EXCLUDE)\b",
}


def _code(sql: str) -> str:
    """`sql` without comments and with every string literal emptied, so that a `;`, `--`, `(` or
    keyword inside one cannot split, hide or fake a statement. A dollar-quoted body is code (a
    function, a DO block): it is cleaned the same way and kept as statements of its own, so the
    rules still read it."""

    def clean(match: re.Match[str]) -> str:
        text = match[0]
        if text.startswith('"'):
            return text
        if text.startswith("'"):
            return "''"
        if text.startswith("$"):
            return f"'';\n{_code(match[2])};\n"
        return " "

    return _LEXEME.sub(clean, sql)


def _actions(code: str) -> list[str]:
    """The actions of every ALTER TABLE in `code`, split on commas outside parentheses."""
    found: list[str] = []
    for statement in code.split(";"):
        head = _ALTER_TABLE.search(statement)
        if head is None:
            continue
        depth, start = 0, head.end()
        for at, char in enumerate(statement[head.end() :], head.end()):
            depth += {"(": 1, ")": -1}.get(char, 0)
            if char == "," and depth == 0:
                found.append(statement[start:at].strip())
                start = at + 1
        found.append(statement[start:].strip())
    return found


def _validated(code: str) -> set[str]:
    return {m[1].lower() for m in _VALIDATE.finditer(code)}


def _check_problems(code: str) -> list[str]:
    """Postgres: a CHECK is added NOT VALID, by name, and validated by a later migration."""
    validated = _validated(code)
    problems: list[str] = []
    for action in _actions(code):
        if not re.match(r"ADD\b", action, _I) or not re.search(r"\bCHECK\b", action, _I):
            continue
        named = _NAMED_CHECK.match(action)
        if not _NOT_VALID.search(action):
            # A column constraint cannot be NOT VALID: add the column, then the constraint.
            problems.append(f"CHECK without NOT VALID: it scans the table locked ({action})")
        elif named is None:
            problems.append(f"unnamed CHECK: a later migration cannot VALIDATE it ({action})")
        elif named[1].lower() in validated:
            problems.append(f"CHECK {named[1]} is validated in the same migration: use a later one")
    return problems


def _compatibility_problems(code: str) -> list[str]:
    """What makes a migration marked compatible_with_previous unsafe for the previous release."""
    found = [what for what, pattern in _NOT_COMPATIBLE.items() if re.search(pattern, code, _I)]
    for action in _actions(code):
        found += [w for w, p in _NOT_COMPATIBLE_ACTION.items() if re.match(p, action, _I)]
        if _ADD_COLUMN.match(action):
            if re.search(r"\bNOT\s+NULL\b", action, _I) and not re.search(
                r"\bDEFAULT\b", action, _I
            ):
                found.append("adds a NOT NULL column without a default")
            if re.search(r"\b(?:CHECK|UNIQUE|PRIMARY\s+KEY)\b", action, _I):
                found.append("adds a column with a constraint")
    return [f"marked compatible_with_previous but {what}" for what in dict.fromkeys(found)]


def lint(dialect: str, migration: Migration) -> list[str]:
    """What is wrong with one migration file; empty when it follows the rules."""
    code = _code(migration.sql)
    problems = _check_problems(code) if dialect == "postgres" else []
    if migration.compatible:
        problems += _compatibility_problems(code)
    return problems


def _example(sql: str, *, compatible: bool = False) -> Migration:
    return Migration(GRANDFATHERED + 1, "example", sql, "x", compatible)


@pytest.mark.parametrize("dialect", ["postgres", "sqlite"])
def test_new_migrations_follow_the_migration_rules(dialect: str) -> None:
    new = [m for m in migrations.load(dialect) if m.version > GRANDFATHERED]
    found = {f"{m.version:04d}_{m.name}": lint(dialect, m) for m in new}
    assert {name: problems for name, problems in found.items() if problems} == {}


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE calls ADD CONSTRAINT c_ok CHECK (n >= 0);",
        'ALTER TABLE calls ADD CONSTRAINT "c_ok" CHECK (n >= 0);',
        "ALTER TABLE calls ADD CHECK (n >= 0);",
        "ALTER TABLE calls ADD CHECK (n >= 0) NOT VALID;",
        "ALTER TABLE calls ADD COLUMN n INT CHECK (n >= 0);",
        "ALTER TABLE calls ADD n INT CHECK (n IN (0, 1));",
        "ALTER TABLE calls ADD COLUMN m INT, ADD CONSTRAINT c_ok CHECK (n IN (1, 2));",
        "ALTER TABLE calls ADD CONSTRAINT c_ok CHECK (n >= 0) NOT VALID;\n"
        "ALTER TABLE calls VALIDATE CONSTRAINT c_ok;",
        "ALTER TABLE calls /* why */ ADD CONSTRAINT c_ok CHECK (n >= 0);",
        "ALTER TABLE calls ADD COLUMN s TEXT DEFAULT ';', ADD CONSTRAINT c_ok CHECK (n >= 0);",
        "ALTER TABLE calls ADD COLUMN s TEXT DEFAULT '--', ADD CONSTRAINT c_ok CHECK (n >= 0);",
        "ALTER TABLE calls ADD COLUMN s TEXT DEFAULT 'it''s (', ADD CHECK (n >= 0);",
        'ALTER TABLE "my calls" ADD CONSTRAINT c_ok CHECK (n >= 0);',
        'ALTER TABLE public."my calls" ADD CONSTRAINT c_ok CHECK (n >= 0);',
        "DO $$ BEGIN ALTER TABLE calls ADD CONSTRAINT c_ok CHECK (n >= 0); END $$;",
    ],
    ids=[
        "no-not-valid",
        "quoted-name",
        "unnamed",
        "unnamed-not-valid",
        "in-add-column",
        "in-add-without-column",
        "second-action",
        "validated-in-the-same-file",
        "block-comment-before-action",
        "semicolon-in-a-default",
        "dashes-in-a-default",
        "escaped-quote-and-paren-in-a-default",
        "quoted-table-name-with-a-space",
        "schema-and-quoted-name",
        "inside-a-do-block",
    ],
)
def test_a_postgres_check_constraint_needs_not_valid_and_a_later_validate(sql: str) -> None:
    assert lint("postgres", _example(sql))


def test_a_not_valid_check_and_its_validate_in_a_later_migration_pass() -> None:
    add = "-- the scan comes later\nALTER TABLE calls ADD CONSTRAINT c_ok CHECK (n >= 0) NOT VALID;"
    assert lint("postgres", _example(add)) == []
    assert (
        lint(
            "postgres",
            _example('ALTER TABLE calls ADD CONSTRAINT "c_ok" CHECK (n >= 0) NOT VALID;'),
        )
        == []
    )
    assert lint("postgres", _example("ALTER TABLE calls VALIDATE CONSTRAINT c_ok;")) == []


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE old_things;",
        "DROP VIEW v;",
        "ALTER TABLE calls DROP COLUMN note;",
        "ALTER TABLE calls DROP note;",
        "ALTER TABLE calls DROP IF EXISTS note;",
        "ALTER TABLE calls ADD COLUMN m TEXT, DROP note;",
        "ALTER TABLE calls RENAME COLUMN note TO remark;",
        "ALTER TABLE calls ALTER COLUMN note TYPE INTEGER;",
        "ALTER TABLE calls ALTER note TYPE INTEGER;",
        "ALTER TABLE calls ALTER COLUMN note SET NOT NULL;",
        "ALTER TABLE calls ALTER COLUMN tries DROP DEFAULT;",
        "DELETE FROM calls WHERE 1 = 1;",
        "TRUNCATE calls;",
        "ALTER TABLE calls ADD COLUMN note TEXT NOT NULL;",
        "ALTER TABLE calls ADD note TEXT NOT NULL;",
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS note TEXT NOT NULL;",
        # Tightening: the previous release may write a row these reject.
        "ALTER TABLE calls ADD CONSTRAINT c_ok CHECK (n >= 0) NOT VALID;",
        "ALTER TABLE calls ADD CHECK (n >= 0);",
        "ALTER TABLE calls ADD CONSTRAINT calls_note UNIQUE (note);",
        "ALTER TABLE calls ADD CONSTRAINT calls_k FOREIGN KEY (k) REFERENCES keys(id) NOT VALID;",
        "ALTER TABLE calls ADD FOREIGN KEY (k) REFERENCES keys(id);",
        "ALTER TABLE calls ADD COLUMN k TEXT REFERENCES keys(id);",
        "ALTER TABLE calls ADD COLUMN n INT CHECK (n >= 0);",
        "ALTER TABLE calls ADD COLUMN note TEXT UNIQUE;",
        "CREATE UNIQUE INDEX calls_note ON calls(note);",
        "CREATE TABLE extra (id TEXT PRIMARY KEY, call_id TEXT REFERENCES calls(id));",
        # Comments, literals and quoted names cannot hide a drop.
        "ALTER TABLE calls /* x */ DROP note;",
        "ALTER TABLE calls ADD COLUMN s TEXT DEFAULT '(', DROP note;",
        "ALTER TABLE calls ADD COLUMN s TEXT DEFAULT ')', DROP note;",
        "ALTER TABLE calls ADD COLUMN s TEXT DEFAULT ';', DROP note;",
        'ALTER TABLE "my calls" DROP note;',
        "DO $body$ BEGIN EXECUTE 'x'; ALTER TABLE calls DROP note; END $body$;",
        # Anything else dropped may be used by the previous release.
        "DROP SEQUENCE call_numbers;",
        "DROP TYPE call_state;",
        "DROP FUNCTION refuse_change();",
        "DROP SCHEMA extras CASCADE;",
        "DROP TRIGGER calls_append_only ON calls;",
        "DROP MATERIALIZED VIEW daily;",
        "ALTER TABLE calls ALTER COLUMN id DROP IDENTITY;",
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
        "ALTER TABLE calls ADD note TEXT;",
        "ALTER TABLE calls ADD COLUMN tries INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE calls ADD COLUMN a TEXT, ADD COLUMN b INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE calls ALTER COLUMN tries SET DEFAULT 1;",
        "ALTER TABLE calls ALTER COLUMN note DROP NOT NULL;",
        "ALTER TABLE calls DROP CONSTRAINT c_old;",
        "CREATE TABLE extra (id TEXT PRIMARY KEY, n INTEGER CHECK (n >= 0));",
        "CREATE INDEX calls_note ON calls(note);",
        "-- we never DROP TABLE here\nCREATE TABLE extra2 (id TEXT);",
        "/* no DROP TABLE either */ CREATE TABLE extra3 (id TEXT);",
        "COMMENT ON TABLE calls IS 'we DROP nothing; -- ( it''s fine';",
        'ALTER TABLE "my calls" ADD COLUMN "the note" TEXT DEFAULT \'a, b\';',
        "DROP INDEX calls_note;",
    ],
)
def test_expand_steps_pass_when_marked_compatible(sql: str) -> None:
    assert lint("postgres", _example(sql, compatible=True)) == []
    assert lint("sqlite", _example(sql, compatible=True)) == []


def test_the_marker_is_read_from_the_file() -> None:
    assert migrations.compatible_marker("-- compatible_with_previous: true\nSELECT 1;")
    assert not migrations.compatible_marker("-- compatible_with_previous: false\nSELECT 1;")
    assert not migrations.compatible_marker("SELECT 1; -- compatible_with_previous: true")
    assert not any(m.compatible for m in migrations.load("postgres")[:GRANDFATHERED])
