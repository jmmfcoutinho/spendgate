"""Read the ledger tables straight from the database (not through the ledger port) into plain data.

Reading with plain SQL keeps the check independent of the code under test. SQLite is opened
read-only; PostgreSQL through psycopg on the run's private schema.
"""

import sqlite3
from decimal import Decimal
from typing import Any

import psycopg

BUDGETS = """
SELECT b.id, s.path, b.cap_nano, COALESCE(p.spent_nano, 0), COALESCE(p.held_nano, 0),
       COALESCE(p.overrun_nano, 0), COALESCE(p.excess_nano, 0), b.review_required_at,
       COALESCE(p.period_start, 0), COALESCE(p.calls_count, 0)
FROM budgets b JOIN scopes s ON s.id = b.scope_id
LEFT JOIN budget_periods p ON p.budget_id = b.id ORDER BY b.id"""
GRANTS = "SELECT COUNT(*) FROM budget_grants"
RESERVATIONS = """
SELECT r.id, r.state, r.charged_nano, r.outcome, COALESCE(SUM(c.charged_nano), 0), COUNT(c.attempt)
FROM reservations r LEFT JOIN calls c ON c.request_id = r.id
GROUP BY r.id, r.state, r.charged_nano, r.outcome"""
ITEMS = """
SELECT i.budget_id, r.state, COUNT(*), SUM(i.held_nano), SUM(COALESCE(r.charged_nano, 0)),
       SUM(i.overrun_nano)
FROM reservation_items i JOIN reservations r ON r.id = i.reservation_id
GROUP BY i.budget_id, r.state"""
OUTCOMES = "SELECT outcome, settled_by, COUNT(*), SUM(charged_nano) FROM calls GROUP BY 1, 2"
ATTEMPTS_SENT = "SELECT COUNT(*) FROM calls WHERE outcome <> 'unreachable' AND settled_by = 'owner'"
MIN_MAX_HOLD = "SELECT MIN(held_nano), MAX(held_nano) FROM reservation_items WHERE held_nano > 0"
PENDING = "SELECT COUNT(*) FROM reservations WHERE state = 'pending'"
PENDING_BY_OWNER = "SELECT owner_instance, id FROM reservations WHERE state = 'pending'"


def _plain(value: Any) -> Any:  # noqa: ANN401 - Decimal (PostgreSQL SUM) to int, rest as is
    return int(value) if isinstance(value, Decimal) else value


def _rows(conn: Any, sql: str) -> list[list[Any]]:  # noqa: ANN401 - sqlite3 and psycopg
    return [[_plain(v) for v in row] for row in conn.execute(sql).fetchall()]


BUDGET_FIELDS = (
    "id", "path", "cap", "spent", "held", "overrun", "excess", "review_at", "period_start",
    "calls_count",
)  # fmt: skip


def _connect(backend: str, location: str) -> Any:  # noqa: ANN401 - sqlite3 or psycopg
    if backend == "sqlite":
        return sqlite3.connect(f"file:{location}?mode=ro", uri=True, timeout=30)
    return psycopg.connect(location, autocommit=True)


def pending_by_owner(backend: str, location: str) -> dict[str, list[str]]:
    """Pending reservation ids per lease owner (`owner_instance`): which gateway holds what."""
    conn = _connect(backend, location)
    try:
        owners: dict[str, list[str]] = {}
        for owner, rid in _rows(conn, PENDING_BY_OWNER):
            owners.setdefault(owner, []).append(rid)
        return owners
    finally:
        conn.close()


def read_facts(backend: str, location: str) -> dict[str, Any]:
    """Everything the checks need, as JSON-friendly lists."""
    conn = _connect(backend, location)
    try:
        return {
            "budgets": [dict(zip(BUDGET_FIELDS, row, strict=True)) for row in _rows(conn, BUDGETS)],
            "grants": _rows(conn, GRANTS)[0][0],
            "reservations": _rows(conn, RESERVATIONS),
            "items": _rows(conn, ITEMS),
            "outcomes": _rows(conn, OUTCOMES),
            "attempts_sent": _rows(conn, ATTEMPTS_SENT)[0][0],
            "pending": _rows(conn, PENDING)[0][0],
            "hold_range": _rows(conn, MIN_MAX_HOLD)[0],
        }
    finally:
        conn.close()
