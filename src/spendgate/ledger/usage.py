# ruff: noqa: S608  (the statement splices whitelist constants and enum values only, never input)
"""Usage reports over `calls`: who spent what, on which model, when.

The query is described by plain data (`UsageQuery`) and turned into one aggregate statement
from a fixed whitelist: every dimension and filter maps to a column expression written in
this file, and every value travels as a bound parameter. Nothing a caller types ever becomes
SQL text (C-11), so a hostile label or model name is inert.

Counting rules, stated once:
- A row of `calls` is one provider attempt, so `calls` counts attempts (a failover is two).
- `tag` is multi-valued: a call with labels {a: 1, b: 2} is counted once under "a=1" and once
  under "b=2", and once under "" when it has none. Rows grouped by tag therefore overlap and
  must not be added up; `UsageResult.total` is computed without the tag expansion and is the
  true figure. (Without `tag` in `group_by`, rows do add up to the total.)
- `usage` basis sums `usage_cost_nano`; a call with no reported usage (NULL) adds 0, so it is
  a lower bound. The `charged` basis is the money booked to budgets.
- Usage covers the retention window only (no rollup in v0.1; see the roadmap).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from spendgate.core.outcomes import Outcome
from spendgate.ledger.sql import Stmt, Value, q

DAY_MS: Final = 86_400_000
MAX_GROUP_BY: Final = 6
MAX_LIMIT: Final = 10_000


class Basis(StrEnum):
    CHARGED = "charged"
    USAGE = "usage"


class UsageQueryError(ValueError):
    """The request is malformed (unknown dimension, too many dimensions, bad limit)."""


class UsageRangeError(UsageQueryError):
    """`since` reaches back past the retention boundary: those days are deleted."""

    def __init__(self, since: int, boundary_ms: int) -> None:
        super().__init__(
            f"since={since} is older than the retention boundary {boundary_ms}: usage covers "
            "the retention window only"
        )
        self.boundary_ms = boundary_ms


@dataclass(frozen=True, slots=True)
class UsageFilters:
    """Equality filters, all optional. `scope` is a scope id or path and includes descendants."""

    scope: str | None = None
    key_id: str | None = None
    model: str | None = None
    provider: str | None = None
    deployment: str | None = None
    user: str | None = None
    session_id: str | None = None
    outcome: Outcome | None = None
    tag: str | None = None  # "name=value", matching one label of the call


@dataclass(frozen=True, slots=True)
class UsageQuery:
    """`[since, until)` is on `finished_at`; either end may be open."""

    group_by: tuple[str, ...] = ()
    since: int | None = None
    until: int | None = None
    filters: UsageFilters = field(default_factory=UsageFilters)
    basis: Basis = Basis.CHARGED
    limit: int = 1_000
    offset: int = 0

    def __post_init__(self) -> None:
        if len(self.group_by) > MAX_GROUP_BY:
            raise UsageQueryError(f"group_by takes at most {MAX_GROUP_BY} dimensions")
        if len(set(self.group_by)) != len(self.group_by):
            raise UsageQueryError("group_by names a dimension twice")
        unknown = [d for d in self.group_by if d not in DIMENSIONS]
        if unknown:
            raise UsageQueryError(f"unknown group_by dimension {unknown[0]!r}")
        if not 1 <= self.limit <= MAX_LIMIT or self.offset < 0:
            raise UsageQueryError(f"limit must be 1..{MAX_LIMIT} and offset >= 0")
        if self.filters.tag is not None and "=" not in self.filters.tag:
            raise UsageQueryError("a tag filter is written name=value")


@dataclass(frozen=True, slots=True)
class UsageRow:
    group: dict[str, str]
    calls: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    tokens_cache_write: int
    tokens_reasoning: int
    audio_ms: int
    characters: int
    cost_nano: int
    outcomes: dict[str, int]  # non-zero counts only


@dataclass(frozen=True, slots=True)
class UsageResult:
    currency: str
    basis: Basis
    since: int | None  # the effective lower bound: never older than the retention boundary
    until: int | None
    rows: tuple[UsageRow, ...]
    total: UsageRow  # the whole filtered window, never double-counted (see the tag rule)
    has_more: bool  # more rows exist past `limit`
    tags_overlap: bool  # `tag` was a dimension, so rows do not add up to `total`


@dataclass(frozen=True, slots=True)
class RetentionResult:
    boundary_ms: int  # everything finished before this UTC midnight is gone
    calls: int
    reservations: int
    sessions: int


def retention_boundary(now: int, days: int) -> int:
    """UTC midnight at or before `now - days`: whole days are kept or deleted, never cut."""
    if days < 1:
        raise ValueError("retention is a whole number of days, at least 1")
    return (now - days * DAY_MS) // DAY_MS * DAY_MS


# --- the whitelist -----------------------------------------------------------------------------

# Join aliases in dependency order. s0 is the call's scope, s1..s3 its ancestors (the tree is at
# most five deep and a call's scope is never the root), ss the session scope, t one label.
_JOINS: Final = {
    "s0": "LEFT JOIN scopes s0 ON s0.id = c.scope_id",
    "s1": "LEFT JOIN scopes s1 ON s1.id = s0.parent_id",
    "s2": "LEFT JOIN scopes s2 ON s2.id = s1.parent_id",
    "s3": "LEFT JOIN scopes s3 ON s3.id = s2.parent_id",
    "ss": "LEFT JOIN scopes ss ON ss.id = c.session_id",
    "tag": "$TAGS",  # dialect specific: one row per label (see sql.Dialect.tag_join)
}
_TAG_EXPR: Final = "t.key || '=' || t.value"


@dataclass(frozen=True, slots=True)
class _Dim:
    expr: str
    joins: tuple[str, ...] = ()


def _ancestor(kind: str, levels: int) -> _Dim:
    """The nearest scope of `kind` at or above the call's scope, looking `levels` deep."""
    parts = ", ".join(f"CASE WHEN s{n}.kind = '{kind}' THEN s{n}.path END" for n in range(levels))
    return _Dim(f"COALESCE({parts}, '')", tuple(f"s{n}" for n in range(levels)))


DIMENSIONS: Final[dict[str, _Dim]] = {
    "scope": _Dim("COALESCE(s0.path, '')", ("s0",)),
    "project": _ancestor("project", 4),
    "service": _ancestor("service", 3),
    "user": _Dim("COALESCE(c.user_label, '')"),
    "session": _Dim("COALESCE(ss.path, '')", ("ss",)),
    "key": _Dim("c.key_id"),
    "model": _Dim("c.model"),
    "provider": _Dim("c.provider"),
    "deployment": _Dim("c.deployment"),
    "outcome": _Dim("c.outcome"),
    "tag": _Dim(f"COALESCE({_TAG_EXPR}, '')", ("tag",)),
    # Integer division on epoch ms gives the UTC midnight; rendered as a date afterwards.
    "day": _Dim(f"(c.finished_at / {DAY_MS}) * {DAY_MS}"),
}

_FILTERS: Final = {
    "key_id": ("c.key_id = :f_key_id", ()),
    "model": ("c.model = :f_model", ()),
    "provider": ("c.provider = :f_provider", ()),
    "deployment": ("c.deployment = :f_deployment", ()),
    "user": ("c.user_label = :f_user", ()),
    "session_id": ("c.session_id = :f_session_id", ()),
    "outcome": ("c.outcome = :f_outcome", ()),
    "tag": (f"{_TAG_EXPR} = :f_tag", ("tag",)),
}
# Descendants by path prefix. substr, not LIKE: LIKE ignores case on SQLite and treats % and _
# in a path as wildcards.
_SCOPE_FILTER: Final = (
    "(s0.path = :scope_path OR substr(s0.path, 1, length(CAST(:scope_path AS TEXT)) + 1) "
    "= CAST(:scope_path AS TEXT) || '/')"
)
_BASIS_COLUMN: Final = {Basis.CHARGED: "charged_nano", Basis.USAGE: "usage_cost_nano"}
_SUMS: Final = (
    "tokens_in",
    "tokens_out",
    "tokens_cached",
    "tokens_cache_write",
    "tokens_reasoning",
    "audio_ms",
    "characters",
)
_OUTCOMES: Final = tuple(Outcome)  # enum values only: a closed set, never input


def _total(column: str, alias: str) -> str:
    # CAST: Postgres sums BIGINT to NUMERIC, which would arrive as Decimal.
    return f"CAST(COALESCE(SUM({column}), 0) AS BIGINT) AS {alias}"


def _measures(basis: Basis) -> list[str]:
    cols = ["COUNT(*) AS calls"]
    cols += [_total(f"c.{name}", name) for name in _SUMS]
    cols.append(_total(f"c.{_BASIS_COLUMN[basis]}", "cost_nano"))
    # COUNT of a CASE with no ELSE counts only the matching rows, cheaper than summing 1/0.
    cols += [
        f"COUNT(CASE WHEN c.outcome = '{o.value}' THEN 1 END) AS o_{o.value}" for o in _OUTCOMES
    ]
    return cols


def build(query: UsageQuery, scope_path: str | None, *, grouped: bool) -> Stmt:
    """The aggregate statement. `grouped=False` is the total: same filters, no dimensions."""
    dims = [DIMENSIONS[d] for d in query.group_by] if grouped else []
    needed: set[str] = {j for d in dims for j in d.joins}
    where: list[str] = []
    params: dict[str, Value] = {}
    if query.since is not None:
        where.append("c.finished_at >= :since")
        params["since"] = query.since
    if query.until is not None:
        where.append("c.finished_at < :until")
        params["until"] = query.until
    if scope_path is not None:
        where.append(_SCOPE_FILTER)
        params["scope_path"] = scope_path
        needed.add("s0")
    for name, (clause, joins) in _FILTERS.items():
        value = getattr(query.filters, name)
        if value is not None:
            where.append(clause)
            params[f"f_{name}"] = str(value)
            needed.update(joins)
    select = [f"{d.expr} AS d{n}" for n, d in enumerate(dims)] + _measures(query.basis)
    joins_sql = " ".join(sql for name, sql in _JOINS.items() if name in needed)
    text = f"SELECT {', '.join(select)} FROM calls c {joins_sql}"
    if where:
        text += " WHERE " + " AND ".join(where)
    if grouped and dims:
        ordinals = ", ".join(str(n + 1) for n in range(len(dims)))
        order = ", ".join(f"d{n}" for n in range(len(dims)))
        text += f" GROUP BY {ordinals} ORDER BY cost_nano DESC, {order} LIMIT :limit OFFSET :offset"
        params["limit"] = query.limit + 1  # one extra row tells whether there is more
        params["offset"] = query.offset
    return q(text, **params)


def _day(ms: Value) -> str:
    if not isinstance(ms, int):
        raise TypeError(f"day bucket is not an integer: {ms!r}")
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def decode_row(row: dict[str, Value], group_by: tuple[str, ...]) -> UsageRow:
    def n(key: str) -> int:
        value = row[key]
        if not isinstance(value, int):
            raise TypeError(f"{key} is not an integer: {value!r}")
        return value

    group = {
        dim: _day(row[f"d{k}"]) if dim == "day" else str(row[f"d{k}"])
        for k, dim in enumerate(group_by)
    }
    outcomes = {o.value: n(f"o_{o.value}") for o in _OUTCOMES if n(f"o_{o.value}")}
    return UsageRow(
        group=group,
        calls=n("calls"),
        tokens_in=n("tokens_in"),
        tokens_out=n("tokens_out"),
        tokens_cached=n("tokens_cached"),
        tokens_cache_write=n("tokens_cache_write"),
        tokens_reasoning=n("tokens_reasoning"),
        audio_ms=n("audio_ms"),
        characters=n("characters"),
        cost_nano=n("cost_nano"),
        outcomes=outcomes,
    )
