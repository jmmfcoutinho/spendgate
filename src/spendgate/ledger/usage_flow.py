"""Flows for usage reports and retention. Each retention batch is its own transaction."""

from spendgate.ledger import sql
from spendgate.ledger.common import s
from spendgate.ledger.port import NotFoundError
from spendgate.ledger.sql import Flow, Row, q
from spendgate.ledger.usage import (
    UsageQuery,
    UsageRangeError,
    UsageResult,
    UsageRow,
    build,
    decode_row,
    retention_boundary,
)


def _stored_boundary() -> Flow[int]:
    rows = yield q(sql.RETENTION_GET)
    return int(s(rows[0], "value")) if rows else 0


def _sum_rows(rows: list[UsageRow]) -> UsageRow:
    outcomes: dict[str, int] = {}
    for row in rows:
        for name, count in row.outcomes.items():
            outcomes[name] = outcomes.get(name, 0) + count
    return UsageRow(
        group={},
        calls=sum(r.calls for r in rows),
        tokens_in=sum(r.tokens_in for r in rows),
        tokens_out=sum(r.tokens_out for r in rows),
        tokens_cached=sum(r.tokens_cached for r in rows),
        tokens_cache_write=sum(r.tokens_cache_write for r in rows),
        tokens_reasoning=sum(r.tokens_reasoning for r in rows),
        audio_ms=sum(r.audio_ms for r in rows),
        characters=sum(r.characters for r in rows),
        cost_nano=sum(r.cost_nano for r in rows),
        outcomes=outcomes,
    )


def usage_flow(
    query: UsageQuery, currency: str, now: int, retention_days: int | None
) -> Flow[UsageResult]:
    # The floor is what retention already deleted, or what a configured retention is about to.
    floor = yield from _stored_boundary()
    if retention_days is not None:
        floor = max(floor, retention_boundary(now, retention_days))
    if query.since is not None and query.since < floor:
        raise UsageRangeError(query.since, floor)
    since = query.since if query.since is not None else (floor or None)
    scope_path: str | None = None
    if query.filters.scope is not None:
        found = yield q(sql.SCOPE_REF, ref=query.filters.scope)
        if not found:
            raise NotFoundError(f"scope {query.filters.scope} does not exist")
        scope_path = s(found[0], "path")
    effective = UsageQuery(
        query.group_by, since, query.until, query.filters, query.basis, query.limit, query.offset
    )
    grouped: list[Row] = []
    if query.group_by:
        grouped = yield build(effective, scope_path, grouped=True)
    rows = [decode_row(r, query.group_by) for r in grouped]
    has_more = len(rows) > query.limit
    rows = rows[: query.limit]
    if query.group_by and not has_more and not query.offset and "tag" not in query.group_by:
        total = _sum_rows(rows)  # the groups partition the calls: no second scan needed
    else:
        total = decode_row((yield build(effective, scope_path, grouped=False))[0], ())
    return UsageResult(
        currency=currency,
        basis=query.basis,
        since=since,
        until=query.until,
        rows=tuple(rows) if query.group_by else (total,),
        total=total,
        has_more=has_more,
        tags_overlap="tag" in query.group_by,
    )


def boundary_flow(boundary: int) -> Flow[None]:
    """Recorded before any deletion, so usage refuses the window while it is being cut."""
    yield q(sql.RETENTION_SET, boundary=str(boundary))


def calls_batch_flow(boundary: int, n: int) -> Flow[int]:
    return len((yield q(sql.CALLS_PURGE, boundary=boundary, n=n)))


def reservations_batch_flow(boundary: int, n: int) -> Flow[int]:
    yield q(sql.RES_ITEMS_PURGE, boundary=boundary, n=n)  # items first: they reference the rows
    return len((yield q(sql.RES_PURGE, boundary=boundary, n=n)))


def sessions_batch_flow(boundary: int, n: int) -> Flow[int]:
    """Delete up to `n` closed sessions with their budgets, periods, grants and reservations."""
    ids = yield q(sql.SESSIONS_OLD, boundary=boundary, n=n)
    for row in ids:
        for statement in sql.SESSION_PURGE:
            yield q(statement, id=s(row, "id"))
    return len(ids)
