# ruff: noqa: S608  (statements splice module-level constants only, never input; see docstring)
"""SQL shared by the SQLite and Postgres backends, written once with named parameters.

Statements use `:name` parameters and the tokens `$LOCK`, `$SKIP_LOCKED`, `$TAGS` and `$JSON_IDS`.
`render` turns them into the driver's own dialect, so the only things that differ between backends
are in `Dialect`.
Parameterised SQL only (C-11): values never get formatted into statement text; the two
f-strings below splice module-level column names, never input.
"""

import re
from collections.abc import Generator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from functools import cache

type Value = int | str | None
type Row = dict[str, Value]


@dataclass(frozen=True, slots=True)
class Dialect:
    name: str
    lock: str  # appended where `$LOCK` stands: row lock on Postgres, nothing on SQLite
    pyformat: bool  # psycopg `%(name)s` instead of `:name`
    tag_join: str = ""  # appended where `$TAGS` stands: joins `t(key, value)`, one row per label
    skip_locked: str = ""  # `$SKIP_LOCKED`: lock the row, or leave it out if another session has
    json_ids: str = ""  # `$JSON_IDS`: the text values of the JSON array parameter `:ids`, as rows


# BEGIN IMMEDIATE already serialises SQLite writers. Tag joins: one row per label of the call, and
# a call with none still yields one row (t.* NULL).
SQLITE = Dialect(
    "sqlite",
    "",
    pyformat=False,
    tag_join="LEFT JOIN json_each(COALESCE(c.labels, '{}')) AS t ON 1",
    json_ids="SELECT value FROM json_each(:ids)",
)
POSTGRES = Dialect(
    "postgres",
    " FOR UPDATE",
    pyformat=True,
    tag_join="LEFT JOIN LATERAL jsonb_each_text(CAST(COALESCE(c.labels, '{}') AS JSONB)) "
    "AS t(key, value) ON TRUE",
    skip_locked=" FOR NO KEY UPDATE SKIP LOCKED",
    json_ids="SELECT jsonb_array_elements_text(CAST(:ids AS JSONB))",
)

_PARAM = re.compile(r"(?<![:\w]):(\w+)")


@cache
def render(sql: str, dialect: Dialect) -> str:
    text = sql.replace("$SKIP_LOCKED", dialect.skip_locked).replace("$LOCK", dialect.lock)
    text = text.replace("$TAGS", dialect.tag_join).replace("$JSON_IDS", dialect.json_ids)
    if dialect.pyformat:
        text = _PARAM.sub(r"%(\1)s", text.replace("%", "%%"))
    return text


@dataclass(frozen=True, slots=True)
class Stmt:
    sql: str
    params: Mapping[str, Value]


class Tx(Enum):
    """Transaction control a flow can ask the driver for (the driver begins the first one)."""

    BEGIN = "begin"
    COMMIT = "commit"
    ROLLBACK = "rollback"


@dataclass(frozen=True, slots=True)
class Batch:
    """Statements that do not depend on each other's rows: Postgres sends them in one round trip.

    They still run one after the other, in order, so locks are taken in the order listed. The
    driver puts one row list per statement into `results` (the flow is sent no rows). `last` marks
    a flow's final writes: the driver resumes the flow first, which must then finish, and sends
    the statements together with the COMMIT, so their results are never seen (see `finish`).
    """

    stmts: tuple[Stmt, ...]
    last: bool = False
    results: list[list[Row]] = field(default_factory=list, compare=False)


# A flow is a transaction written without I/O: it yields statements, batches of them (or
# transaction control) and is sent the rows each one returned. Both backends run the same flows.
type Flow[T] = Generator[Stmt | Batch | Tx, list[Row], T]


def q(sql: str, **params: Value) -> Stmt:
    return Stmt(sql, params)


def batch(*stmts: Stmt) -> Generator[Batch, list[Row], list[list[Row]]]:
    """Run independent statements together; their rows, one list per statement, in order."""
    sent = Batch(stmts)
    yield sent
    if len(sent.results) != len(stmts):
        raise RuntimeError("the driver did not return one row list per batched statement")
    return sent.results


def end_of[T](flow: Flow[T]) -> T:
    """The driver's half of `finish`: resume the flow after its last batch; it must return."""
    try:
        flow.send([])
    except StopIteration as done:
        result: T = done.value
        return result
    raise RuntimeError("a flow yielded a statement after its last batch")


def finish(*stmts: Stmt) -> Generator[Batch, list[Row]]:
    """A flow's last statements (writes whose rows it does not need), sent with the COMMIT.

    Nothing may be yielded after this: the flow returns, and only then are these sent.
    """
    if stmts:
        yield Batch(stmts, last=True)


# --- reservations ----------------------------------------------------------------------------

RES_EXISTS = "SELECT 1 AS x FROM reservations WHERE id = :id"
RES_INSERT = """INSERT INTO reservations (id, state, owner_instance, lease_expires_at, key_id,
  scope_id, session_id, meta, created_at)
  VALUES (:id, 'pending', :owner, :lease, :key_id, :scope_id, :session_id, :meta, :now)"""
ITEM_INSERT = """INSERT INTO reservation_items (reservation_id, budget_id, period_start, held_nano)
  VALUES (:id, :budget_id, :period_start, :held)"""
ITEMS_OF = """SELECT budget_id, period_start, held_nano, overrun_nano FROM reservation_items
  WHERE reservation_id = :id ORDER BY budget_id"""
ITEM_OVERRUN = """UPDATE reservation_items SET overrun_nano = overrun_nano + :over
  WHERE reservation_id = :id AND budget_id = :budget_id"""
# The state-guarded UPDATE is the whole idempotency mechanism: 0 rows = already terminal.
_GUARDED = "RETURNING key_id, scope_id, session_id, meta, created_at"
RES_SETTLE = f"""UPDATE reservations SET state = 'settled', outcome = :outcome,
  charged_nano = :charged, finished_at = :now WHERE id = :id AND state = 'pending' {_GUARDED}"""
RES_RELEASE = f"""UPDATE reservations SET state = 'released', outcome = :outcome,
  charged_nano = 0, finished_at = :now WHERE id = :id AND state = 'pending' {_GUARDED}"""
RES_EXPIRE = f"""UPDATE reservations SET state = 'expired', outcome = 'expired', finished_at = :now,
  charged_nano = (SELECT COALESCE(MAX(held_nano), 0) FROM reservation_items
                  WHERE reservation_id = reservations.id)
  WHERE id = :id AND state = 'pending' AND lease_expires_at < :now {_GUARDED}"""
# Late settle: the reservation shows what was booked, the swept hold plus the overrun (M-04).
RES_LATE = "UPDATE reservations SET charged_nano = :charged WHERE id = :id AND state = 'expired'"
RES_GET = """SELECT id, state, owner_instance, lease_expires_at, key_id, scope_id, session_id,
  outcome, charged_nano, meta, created_at FROM reservations WHERE id = :id"""
# One statement for every hold the caller names (a JSON array in `:ids`): only those it still
# owns, as a hold nobody owns must expire. A locked row is skipped, not waited for (up to
# lock_timeout, failing the whole heartbeat): it is being closed by its owner, or expired by a
# sweeper because its lease is already overdue.
RES_RENEW = """UPDATE reservations SET lease_expires_at = :until
  WHERE id IN (SELECT id FROM reservations
    WHERE id IN ($JSON_IDS) AND owner_instance = :owner AND state = 'pending'
    ORDER BY id $SKIP_LOCKED) RETURNING id"""
RES_DUE = """SELECT id FROM reservations WHERE state = 'pending' AND lease_expires_at < :now
  ORDER BY lease_expires_at, id LIMIT :limit"""
SESSION_PENDING = """SELECT COUNT(*) AS n FROM reservations
  WHERE session_id = :id AND state = 'pending'"""

# --- scopes and sessions ---------------------------------------------------------------------

_SCOPE_COLS = """id, kind, parent_id, name, path, step_count, max_steps, max_seconds, idle_seconds,
  started_at, last_activity_at, closed_at, closed_reason, created_at, display_name"""
SCOPE_GET = f"SELECT {_SCOPE_COLS} FROM scopes WHERE id = :id"
SCOPE_LOCK = "SELECT id FROM scopes WHERE id = :id $LOCK"
SCOPE_FIND = f"SELECT {_SCOPE_COLS} FROM scopes WHERE path = :path"
SCOPE_BY_PATH = f"SELECT {_SCOPE_COLS} FROM scopes WHERE path = :path $LOCK"
SCOPE_INSERT = """INSERT INTO scopes (id, kind, parent_id, name, path, created_at, max_steps,
  max_seconds, idle_seconds, step_count, started_at, last_activity_at)
  VALUES (:id, :kind, :parent_id, :name, :path, :now, :max_steps, :max_seconds, :idle_seconds,
          0, :started_at, :last_activity_at)"""
CHAIN = """WITH RECURSIVE chain(id, parent_id, kind, path, disabled_at, depth) AS (
  SELECT id, parent_id, kind, path, disabled_at, 0 FROM scopes WHERE id = :leaf
  UNION ALL
  SELECT s.id, s.parent_id, s.kind, s.path, s.disabled_at, c.depth + 1
  FROM scopes s JOIN chain c ON s.id = c.parent_id WHERE c.depth < 5
) SELECT id, kind, path, disabled_at FROM chain ORDER BY depth"""
SCOPE_DISABLE = "UPDATE scopes SET disabled_at = :now WHERE id = :id AND disabled_at IS NULL"
META_INIT = (
    "INSERT INTO ledger_meta (key, value) VALUES ('currency', :currency) ON CONFLICT DO NOTHING"
)
META_GET = "SELECT value FROM ledger_meta WHERE key = 'currency'"
SESSION_CLOSE = """UPDATE scopes SET closed_at = :now, closed_reason = :reason
  WHERE id = :id AND closed_at IS NULL"""
# Idle restart: the old row keeps its history under a new name; the original name is reused.
SESSION_RENAME = """UPDATE scopes SET closed_at = :now, name = name || '#' || :suffix,
  path = path || '#' || :suffix WHERE id = :id"""
SESSION_TOUCH = """UPDATE scopes SET step_count = step_count + 1, last_activity_at = :now
  WHERE id = :id"""
SESSION_REFUND = "UPDATE scopes SET step_count = step_count - 1 WHERE id = :id AND step_count > 0"
SESSION_NEW_COUNT = """SELECT COUNT(*) AS n FROM scopes
  WHERE kind = 'session' AND parent_id = :parent
  AND substr(name, 1, :plen) = :prefix AND created_at > :since"""

# --- budgets, periods, grants ----------------------------------------------------------------

_BUDGET_COLS = """id, scope_id, period, cap_nano, soft_pct, model_filter, strict_overrun,
  review_required_at, disabled_at"""
BUDGET_LOCK = f"SELECT {_BUDGET_COLS} FROM budgets WHERE id = :id $LOCK"
BUDGET_GET = f"SELECT {_BUDGET_COLS} FROM budgets WHERE id = :id"  # reports only: no lock
# Settle and release lock every budget of a reservation in one statement, ascending by id (the
# lock order), then read their grants and current periods under those locks.
_ITEM_BUDGETS = "SELECT budget_id FROM reservation_items WHERE reservation_id = :id"
ITEM_BUDGETS_LOCK = (
    f"SELECT {_BUDGET_COLS} FROM budgets WHERE id IN ({_ITEM_BUDGETS}) ORDER BY id $LOCK"
)
ITEM_GRANTS = f"""SELECT budget_id, amount_nano, valid_from, valid_until FROM budget_grants
  WHERE budget_id IN ({_ITEM_BUDGETS})"""
ITEM_PERIODS = """SELECT p.budget_id, p.spent_nano, p.held_nano, p.overrun_nano, p.excess_nano,
  p.soft_alerted_at FROM budget_periods p JOIN reservation_items it
  ON it.budget_id = p.budget_id AND it.period_start = p.period_start
  WHERE it.reservation_id = :id"""
BUDGET_INSERT = """INSERT INTO budgets (id, scope_id, period, cap_nano, soft_pct, model_filter,
  strict_overrun, note, created_at)
  VALUES (:id, :scope_id, :period, :cap, :soft_pct, :model_filter, :strict, :note, :now)"""
BUDGET_UPDATE = """UPDATE budgets SET cap_nano = COALESCE(:cap, cap_nano),
  soft_pct = COALESCE(:soft_pct, soft_pct), strict_overrun = COALESCE(:strict, strict_overrun)
  WHERE id = :id"""
BUDGET_FLAG = """UPDATE budgets SET review_required_at = :now
  WHERE id = :id AND review_required_at IS NULL RETURNING id"""
BUDGET_DISABLE = "UPDATE budgets SET disabled_at = :now WHERE id = :id AND disabled_at IS NULL"
BUDGET_ACK = "UPDATE budgets SET review_required_at = NULL WHERE id = :id"
BUDGETS_OF_SCOPE = (
    "SELECT id FROM budgets WHERE scope_id = :scope AND disabled_at IS NULL ORDER BY id"
)
CHAIN_BUDGETS = """WITH RECURSIVE chain(id, parent_id, depth) AS (
  SELECT id, parent_id, 0 FROM scopes WHERE id = :leaf
  UNION ALL
  SELECT s.id, s.parent_id, c.depth + 1 FROM scopes s JOIN chain c ON s.id = c.parent_id
  WHERE c.depth < 5
) SELECT b.id, b.scope_id, b.model_filter, b.period FROM budgets b
  WHERE b.disabled_at IS NULL AND b.scope_id IN (SELECT id FROM chain) ORDER BY b.id"""
GRANTS_OF = "SELECT amount_nano, valid_from, valid_until FROM budget_grants WHERE budget_id = :id"
GRANT_INSERT = """INSERT INTO budget_grants (id, budget_id, amount_nano, valid_from, valid_until,
  created_by, created_at, reason) VALUES (:id, :budget_id, :amount, :valid_from, :valid_until,
  :actor, :now, :reason)"""
GRANT_EXPIRED = """SELECT DISTINCT budget_id FROM budget_grants WHERE valid_until <= :now
  ORDER BY budget_id"""
PERIOD_ENSURE = """INSERT INTO budget_periods (budget_id, period_start)
  VALUES (:budget_id, :period_start) ON CONFLICT DO NOTHING"""
# The same, sent before the budget's row is seen: only for a budget that exists and is enabled.
PERIOD_ENSURE_LIVE = """INSERT INTO budget_periods (budget_id, period_start)
  SELECT id, :period_start FROM budgets WHERE id = :budget_id AND disabled_at IS NULL
  ON CONFLICT DO NOTHING"""
_PERIOD_COLS = "period_start, spent_nano, held_nano, overrun_nano, excess_nano, calls_count"
PERIOD_GET = f"""SELECT {_PERIOD_COLS}, soft_alerted_at FROM budget_periods
  WHERE budget_id = :budget_id AND period_start = :period_start"""
PERIODS_OF = f"""SELECT {_PERIOD_COLS} FROM budget_periods
  WHERE budget_id = :budget_id ORDER BY period_start"""
_PERIOD_KEY = "WHERE budget_id = :budget_id AND period_start = :period_start"
PERIOD_HOLD = f"""UPDATE budget_periods SET held_nano = held_nano + :worst, excess_nano = :excess,
  soft_alerted_at = COALESCE(soft_alerted_at, :soft) {_PERIOD_KEY}"""
# Settle and expire: the hold leaves `held`, the charge enters `spent`; the extra over the hold
# is an overrun (M-03: always booked, never clamped).
PERIOD_BOOK = f"""UPDATE budget_periods SET held_nano = held_nano - :held,
  spent_nano = spent_nano + :charge, overrun_nano = overrun_nano + :over,
  calls_count = calls_count + 1 {_PERIOD_KEY}"""
PERIOD_RELEASE = f"UPDATE budget_periods SET held_nano = held_nano - :held {_PERIOD_KEY}"
PERIOD_LATE = f"""UPDATE budget_periods SET spent_nano = spent_nano + :over,
  overrun_nano = overrun_nano + :over {_PERIOD_KEY}"""
PERIOD_EXCESS = f"UPDATE budget_periods SET excess_nano = :excess {_PERIOD_KEY}"

# --- calls -----------------------------------------------------------------------------------

_ATTEMPT_COLS = [
    "attempt",
    "provider",
    "deployment",
    "price_id",
    "tokens_in",
    "tokens_out",
    "tokens_cached",
    "tokens_cache_write",
    "tokens_reasoning",
    "audio_tokens_in",
    "audio_tokens_out",
    "audio_ms",
    "characters",
    "images",
    "native_currency",
    "native_cost_nano",
    "fx_rate_nano",
    "charged_nano",
    "usage_cost_nano",
    "overrun_nano",
    "http_status",
    "outcome",
    "error_code",
    "streamed",
    "review_required_at",
    "started_at",
    "finished_at",
    "latency_ms",
    "ttfb_ms",
]
_COMMON_COLS = [
    "request_id",
    "key_id",
    "scope_id",
    "session_id",
    "user_label",
    "labels",
    "endpoint",
    "model",
    "catalog_sha",
    "hold_nano",
    "settled_by",
]
_ALL_CALL_COLS = _COMMON_COLS + _ATTEMPT_COLS
CALL_INSERT = (
    f"INSERT INTO calls ({', '.join(_ALL_CALL_COLS)}) "
    f"VALUES ({', '.join(':' + c for c in _ALL_CALL_COLS)})"
)
# Late settle rewrites the sweeper's row into the attempt that consumed the hold.
CALL_LATE = (
    f"UPDATE calls SET {', '.join(f'{c} = :{c}' for c in _ATTEMPT_COLS)} "
    "WHERE request_id = :request_id AND attempt = :old_attempt AND outcome = 'expired' "
    "RETURNING attempt"
)
CALL_SWEEPER_ROW = """SELECT attempt FROM calls
  WHERE request_id = :id AND settled_by = 'sweeper' AND outcome = 'expired'"""
CALLS_OF = """SELECT request_id, attempt, provider, deployment, outcome, charged_nano,
  usage_cost_nano, hold_nano, overrun_nano, settled_by, review_required_at, finished_at,
  http_status FROM calls WHERE request_id = :id ORDER BY attempt"""

# --- kill switches, keys, audit --------------------------------------------------------------

KILL_ACTIVE = (
    "SELECT id, target_kind, target_ref FROM kill_switches WHERE cleared_at IS NULL ORDER BY id"
)
KILL_INSERT = """INSERT INTO kill_switches
  (id, target_kind, target_ref, reason, created_by, created_at)
  VALUES (:id, :kind, :ref, :reason, :actor, :now)"""
KILL_GET = "SELECT id FROM kill_switches WHERE id = :id"
KILL_LIFT = "UPDATE kill_switches SET cleared_at = :now WHERE id = :id AND cleared_at IS NULL"
# `secret_hash` '!' can never equal a hex digest: this row exists for the foreign key only.
KEY_INSERT = """INSERT INTO keys (id, prefix, secret_hash, scope_id, name, created_at)
  VALUES (:id, :id, '!', :scope_id, :id, :now)"""
AUDIT_INSERT = """INSERT INTO admin_audit (id, at, actor_key_id, action, target_type, target_id,
  before, after) VALUES (:id, :now, :actor, :action, :target_type, :target_id, :before, :after)"""

# --- keys --------------------------------------------------------------------------

_KEY_COLS = """id, prefix, secret_hash, kind, scope_id, name, allowed_models, limits,
  session_defaults, admin_perms, created_at, expires_at, revoked_at, replaces_key_id"""
KEY_FULL_INSERT = """INSERT INTO keys (id, prefix, secret_hash, kind, scope_id, name,
  allowed_models, limits, session_defaults, admin_perms, created_at, expires_at, replaces_key_id)
  VALUES (:id, :prefix, :secret_hash, :kind, :scope_id, :name, :allowed_models, :limits,
  :session_defaults, :admin_perms, :now, :expires_at, :replaces_key_id)"""
KEY_BY_PREFIX = f"SELECT {_KEY_COLS} FROM keys WHERE prefix = :prefix"
KEY_GET = f"SELECT {_KEY_COLS} FROM keys WHERE id = :id"
KEY_LOCK = f"SELECT {_KEY_COLS} FROM keys WHERE id = :id $LOCK"
KEY_LIST = f"SELECT {_KEY_COLS} FROM keys WHERE kind = :kind ORDER BY created_at, id"
KEY_LIST_SCOPE = (
    f"SELECT {_KEY_COLS} FROM keys WHERE kind = :kind AND scope_id = :scope_id "
    "ORDER BY created_at, id"
)
KEY_REVOKE = "UPDATE keys SET revoked_at = :now WHERE id = :id AND revoked_at IS NULL"
KEY_SET_EXPIRY = "UPDATE keys SET expires_at = :expires_at WHERE id = :id"
KEY_SET_POLICY = """UPDATE keys SET name = :name, allowed_models = :allowed_models,
  limits = :limits, session_defaults = :session_defaults WHERE id = :id"""

# --- usage and retention ------------------------------------------------------------

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

# --- admin API reads and extra writes ----------------------------------------------
# CAST on nullable filters: Postgres cannot infer the type of a bare `NULL IS NULL` parameter.

# Admin-created scopes carry a display name; session scopes (SCOPE_INSERT) never do.
SCOPE_INSERT_NAMED = """INSERT INTO scopes (id, kind, parent_id, name, path, created_at,
  display_name, step_count)
  VALUES (:id, :kind, :parent_id, :name, :path, :now, :display_name, 0)"""
SCOPE_BY_REF = f"SELECT {_SCOPE_COLS} FROM scopes WHERE id = :ref OR path = :ref"
SCOPE_SET_NAME = "UPDATE scopes SET display_name = :display_name WHERE id = :id"
SCOPES_LIST = f"""SELECT {_SCOPE_COLS} FROM scopes
  WHERE (CAST(:kind AS TEXT) IS NULL OR kind = CAST(:kind AS TEXT))
  AND (CAST(:parent AS TEXT) IS NULL OR parent_id = CAST(:parent AS TEXT))
  AND id > :after ORDER BY id LIMIT :limit"""
BUDGET_NOTE = "SELECT note, disabled_at FROM budgets WHERE id = :id"
BUDGET_SET_NOTE = "UPDATE budgets SET note = :note WHERE id = :id"
BUDGETS_ALL = "SELECT id FROM budgets WHERE disabled_at IS NULL ORDER BY id"
KILL_LIST = """SELECT id, target_kind, target_ref, reason, created_by, created_at, cleared_at
  FROM kill_switches WHERE cleared_at IS NULL ORDER BY created_at, id"""
KILL_GLOBAL = """SELECT 1 AS x FROM kill_switches
  WHERE target_kind = 'global' AND cleared_at IS NULL LIMIT 1"""
PENDING_COUNT = "SELECT COUNT(*) AS n FROM reservations WHERE state = 'pending'"
AUDIT_LIST = """SELECT id, at, actor_key_id, action, target_type, target_id, before, after
  FROM admin_audit WHERE at >= :since AND at < :until
  AND (CAST(:actor AS TEXT) IS NULL OR actor_key_id = CAST(:actor AS TEXT))
  AND (at < :cursor_at OR (at = :cursor_at AND id < :cursor_id))
  ORDER BY at DESC, id DESC LIMIT :limit"""
_CALL_SELECT = f"SELECT {', '.join(_ALL_CALL_COLS)} FROM calls"
CALLS_LIST = f"""{_CALL_SELECT}
  WHERE (CAST(:scope_id AS TEXT) IS NULL OR scope_id = CAST(:scope_id AS TEXT))
  AND (CAST(:key_id AS TEXT) IS NULL OR key_id = CAST(:key_id AS TEXT))
  AND (CAST(:session_id AS TEXT) IS NULL OR session_id = CAST(:session_id AS TEXT))
  AND (CAST(:outcome AS TEXT) IS NULL OR outcome = CAST(:outcome AS TEXT))
  AND finished_at >= :since AND finished_at < :until
  AND (finished_at < :cf OR (finished_at = :cf AND (request_id < :cr
       OR (request_id = :cr AND attempt < :ca))))
  ORDER BY finished_at DESC, request_id DESC, attempt DESC LIMIT :limit"""
CALL_LAST = f"{_CALL_SELECT} WHERE request_id = :id ORDER BY attempt DESC LIMIT 1"
