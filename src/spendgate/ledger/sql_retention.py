# ruff: noqa: S608  (statements splice module-level constants only, never input)
"""SQL for usage scope lookups and retention purges, shared by both backends (see `sql`).

Kept apart from `sql` so each module stays readable; the same rules apply: `:name` parameters,
values never formatted into statement text, f-strings splice module-level constants only.
"""

SCOPE_REF = "SELECT path FROM scopes WHERE id = :ref OR path = :ref"
RETENTION_GET = "SELECT value FROM ledger_meta WHERE key = 'retention_boundary'"
# Monotonic: a shorter retention later cannot move the boundary back over deleted days.
RETENTION_SET = """INSERT INTO ledger_meta (key, value) VALUES ('retention_boundary', :boundary)
  ON CONFLICT (key) DO UPDATE SET value = CASE
    WHEN CAST(excluded.value AS BIGINT) > CAST(ledger_meta.value AS BIGINT) THEN excluded.value
    ELSE ledger_meta.value END"""
# Row-value IN keeps each batch bounded without a driver-specific DELETE ... LIMIT.
CALLS_PURGE = """DELETE FROM calls WHERE (request_id, attempt) IN (
  SELECT request_id, attempt FROM calls WHERE finished_at < :boundary
  ORDER BY finished_at, request_id, attempt LIMIT :n) RETURNING 1 AS n"""
_OLD_RESERVATIONS = """SELECT id FROM reservations WHERE state <> 'pending'
  AND finished_at < :boundary ORDER BY finished_at, id LIMIT :n"""
RES_ITEMS_PURGE = f"DELETE FROM reservation_items WHERE reservation_id IN ({_OLD_RESERVATIONS})"
RES_PURGE = f"DELETE FROM reservations WHERE id IN ({_OLD_RESERVATIONS}) RETURNING 1 AS n"
# Closed sessions only, and never one that still has a pending hold.
SESSIONS_OLD = """SELECT id FROM scopes WHERE kind = 'session' AND closed_at < :boundary
  AND NOT EXISTS (SELECT 1 FROM reservations r WHERE r.session_id = scopes.id
                  AND r.state = 'pending') ORDER BY closed_at, id LIMIT :n"""
_SESSION_BUDGETS = "SELECT id FROM budgets WHERE scope_id = :id"
SESSION_PURGE = (
    "DELETE FROM reservation_items WHERE reservation_id IN "
    "(SELECT id FROM reservations WHERE session_id = :id)",
    "DELETE FROM reservations WHERE session_id = :id",
    f"DELETE FROM reservation_items WHERE budget_id IN ({_SESSION_BUDGETS})",
    f"DELETE FROM budget_periods WHERE budget_id IN ({_SESSION_BUDGETS})",
    f"DELETE FROM budget_grants WHERE budget_id IN ({_SESSION_BUDGETS})",
    "DELETE FROM budgets WHERE scope_id = :id",
    "DELETE FROM scopes WHERE id = :id",
)
