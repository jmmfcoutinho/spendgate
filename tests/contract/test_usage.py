"""Ledger contract: usage reports and retention. Runs on every adapter."""

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spendgate.core.budgets import Period, ScopeKind
from spendgate.core.outcomes import Outcome
from spendgate.core.policy import KeyPolicy
from spendgate.core.sessions import SessionDefaults
from spendgate.ledger.port import NotFoundError
from spendgate.ledger.usage import (
    DAY_MS,
    Basis,
    UsageFilters,
    UsageQuery,
    UsageQueryError,
    UsageRangeError,
    UsageResult,
    retention_boundary,
)
from tests.contract.rig import T0, E, Rig, World, attempt

D0 = T0 // DAY_MS * DAY_MS  # a UTC midnight
D1 = D0 + DAY_MS


@dataclass(frozen=True, slots=True)
class Seeded:
    rig: Rig
    api: World
    web: World
    scopes: tuple[str, str]


def make_tree(rig: Rig) -> tuple[World, World]:
    """org > project:app > service:{api,web} > user:u1, each user with its own key and budget."""
    org = rig.run(rig.ledger.create_scope(ScopeKind.ORG, "acme", None, now=T0))
    project = rig.run(rig.ledger.create_scope(ScopeKind.PROJECT, "app", org.id, now=T0))
    worlds: list[World] = []
    for service_name, key_id in (("api", "key-a"), ("web", "key-b")):
        service = rig.run(
            rig.ledger.create_scope(ScopeKind.SERVICE, service_name, project.id, now=T0)
        )
        user = rig.run(rig.ledger.create_scope(ScopeKind.USER, "u1", service.id, now=T0))
        budget = rig.run(rig.ledger.create_budget(user.id, Period.NONE, E("1000"), now=T0))
        rig.run(rig.ledger.add_key_record(key_id, user.id, now=T0))
        worlds.append(World(user, budget, KeyPolicy(key_id, user.id), service))
    return worlds[0], worlds[1]


def call(  # noqa: PLR0913 - one realistic call: a real hold, then a real settle
    rig: Rig,
    w: World,
    rid: str,
    when: int,
    charge: str,
    *,
    usage: str | None = None,
    outcome: Outcome = Outcome.OK,
    model: str = "gpt",
    provider: str = "p1",
    user: str | None = None,
    labels: dict[str, str] | None = None,
    session: str | None = None,
    key: KeyPolicy | None = None,
) -> None:
    rig.hold_ok(
        w,
        rid,
        "5",
        when,
        model=model,
        providers=(provider,),
        labels=labels,
        user_label=user,
        session=session,
        key=key,
    )
    done = attempt(
        1,
        outcome,
        E(charge),
        when,
        usage_cost_nano=E(usage) if usage is not None else None,
        provider=provider,
    )
    rig.run(rig.ledger.settle(rid, E(charge), (done,), when))


@pytest.fixture
def seeded(rig: Rig) -> Seeded:
    api, web = make_tree(rig)
    call(rig, api, "c1", D0 + 1_000, "1.00", usage="0.90", user="alice",
         labels={"team": "x", "env": "prod"})  # fmt: skip
    call(rig, api, "c2", D0 + 2_000, "2.00", usage="1.50", model="claude", provider="p2",
         user="alice", labels={"team": "x"})  # fmt: skip
    call(rig, web, "c3", D1 + 1_000, "4.00", user="bob", outcome=Outcome.OK_NO_USAGE)
    call(rig, web, "c4", D1 + 2_000, "0.50", user="bob", outcome=Outcome.TIMEOUT)
    return Seeded(rig, api, web, (api.scope.id, web.scope.id))


def usage(rig: Rig, **kwargs: object) -> UsageResult:
    query = UsageQuery(**kwargs)  # type: ignore[arg-type]
    return rig.run(rig.ledger.usage(query, now=D1 + DAY_MS))


def costs(result: UsageResult) -> dict[tuple[str, ...], tuple[int, int]]:
    return {tuple(r.group.values()): (r.calls, r.cost_nano) for r in result.rows}


# --- grouping --------------------------------------------------------------------------------

PATH_API = "org:acme/project:app/service:api/user:u1"
PATH_WEB = "org:acme/project:app/service:web/user:u1"
EXPECTED = {
    "scope": {(PATH_API,): (2, E("3")), (PATH_WEB,): (2, E("4.5"))},
    "project": {("org:acme/project:app",): (4, E("7.5"))},
    "service": {
        ("org:acme/project:app/service:api",): (2, E("3")),
        ("org:acme/project:app/service:web",): (2, E("4.5")),
    },
    "user": {("alice",): (2, E("3")), ("bob",): (2, E("4.5"))},
    "key": {("key-a",): (2, E("3")), ("key-b",): (2, E("4.5"))},
    "model": {("gpt",): (3, E("5.5")), ("claude",): (1, E("2"))},
    "provider": {("p1",): (3, E("5.5")), ("p2",): (1, E("2"))},
    "deployment": {("p1-dep1",): (3, E("5.5")), ("p2-dep1",): (1, E("2"))},
    "outcome": {
        ("ok",): (2, E("3")),
        ("ok_no_usage",): (1, E("4")),
        ("timeout",): (1, E("0.5")),
    },
    "day": {
        ("2023-11-14",): (2, E("3")),
        ("2023-11-15",): (2, E("4.5")),
    },
    "tag": {
        ("team=x",): (2, E("3")),
        ("env=prod",): (1, E("1")),
        ("",): (2, E("4.5")),  # calls without labels
    },
}


def test_day_expectations_are_the_utc_dates_of_the_seed() -> None:
    assert datetime.fromtimestamp(D0 / 1000, UTC).strftime("%Y-%m-%d") == "2023-11-14"


@pytest.mark.parametrize("dimension", sorted(EXPECTED))
def test_group_by_each_dimension(seeded: Seeded, dimension: str) -> None:
    result = usage(seeded.rig, group_by=(dimension,))
    assert costs(result) == EXPECTED[dimension]
    assert result.total.calls == 4
    assert result.total.cost_nano == E("7.5")
    assert result.tags_overlap == (dimension == "tag")


def test_group_by_session_uses_the_session_path(rig: Rig) -> None:
    w = rig.world("1000", session_defaults=SessionDefaults(max_steps=5))
    call(rig, w, "s-1", T0, "1.00", usage="1.00", session="chat")
    call(rig, w, "s-2", T0 + 1, "2.00", usage="2.00", session="chat")
    call(rig, w, "plain", T0 + 2, "0.25", usage="0.25", key=KeyPolicy("key-1", w.scope.id))
    result = usage(rig, group_by=("session",))
    assert costs(result) == {
        ("org:acme/user:u1/session:key-1:chat",): (2, E("3")),
        ("",): (1, E("0.25")),
    }


def test_several_dimensions_at_once_and_the_cap_of_six(seeded: Seeded) -> None:
    result = usage(seeded.rig, group_by=("service", "model", "day"))
    assert len(result.rows) == 3  # c3 and c4 share service, model and day
    assert {"service", "model", "day"} == set(result.rows[0].group)
    six = ("scope", "key", "service", "user", "model", "provider")
    assert usage(seeded.rig, group_by=six).total.calls == 4
    with pytest.raises(UsageQueryError, match="at most 6"):
        UsageQuery(group_by=(*six, "day"))
    with pytest.raises(UsageQueryError, match="unknown"):
        UsageQuery(group_by=("password",))
    with pytest.raises(UsageQueryError, match="twice"):
        UsageQuery(group_by=("model", "model"))


def test_a_call_with_several_tags_counts_under_each_but_not_twice_in_the_total(
    seeded: Seeded,
) -> None:
    result = usage(seeded.rig, group_by=("tag",))
    assert sum(r.calls for r in result.rows) == 5  # c1 appears under two tags
    assert result.total.calls == 4
    assert result.tags_overlap


def test_tokens_and_outcome_counts_are_summed(seeded: Seeded) -> None:
    only = usage(seeded.rig, group_by=("provider",)).rows[0]
    assert only.group == {"provider": "p1"}
    assert (only.tokens_in, only.tokens_out) == (10, 5)  # only c1 reported usage
    assert only.outcomes == {"ok": 1, "ok_no_usage": 1, "timeout": 1}
    assert usage(seeded.rig).total.outcomes["ok"] == 2


def test_no_group_by_is_one_total_row(seeded: Seeded) -> None:
    result = usage(seeded.rig)
    assert result.rows == (result.total,)
    assert result.rows[0].group == {}


# --- basis, windows, filters -----------------------------------------------------------------


def test_basis_charged_versus_usage(seeded: Seeded) -> None:
    charged = usage(seeded.rig, group_by=("key",))
    by_usage = usage(seeded.rig, group_by=("key",), basis=Basis.USAGE)
    assert charged.basis is Basis.CHARGED
    assert costs(charged)[("key-b",)] == (2, E("4.5"))  # the full hold counts as booked
    assert costs(by_usage) == {("key-a",): (2, E("2.4")), ("key-b",): (2, 0)}  # NULL adds 0


def test_since_is_inclusive_until_exclusive(seeded: Seeded) -> None:
    first_day = usage(seeded.rig, group_by=("day",), since=D0, until=D1)
    assert first_day.total.calls == 2
    assert usage(seeded.rig, since=D0 + 2_000, until=D0 + 2_001).total.calls == 1
    assert usage(seeded.rig, since=D0 + 2_001, until=D0 + 2_001).total.calls == 0


def test_filters(seeded: Seeded) -> None:
    rig = seeded.rig
    assert usage(rig, filters=UsageFilters(model="claude")).total.cost_nano == E("2")
    assert usage(rig, filters=UsageFilters(key_id="key-b")).total.calls == 2
    assert usage(rig, filters=UsageFilters(user="bob", outcome=Outcome.TIMEOUT)).total.calls == 1
    assert usage(rig, filters=UsageFilters(provider="p2")).total.calls == 1
    assert usage(rig, filters=UsageFilters(tag="env=prod")).total.calls == 1
    by_tag = usage(rig, group_by=("model",), filters=UsageFilters(tag="team=x"))
    assert costs(by_tag) == {("gpt",): (1, E("1")), ("claude",): (1, E("2"))}


def test_scope_filter_takes_an_id_or_a_path_and_includes_descendants(seeded: Seeded) -> None:
    rig = seeded.rig
    service = seeded.api.parent
    assert usage(rig, filters=UsageFilters(scope=service.id)).total.calls == 2
    assert usage(rig, filters=UsageFilters(scope=service.path)).total.cost_nano == E("3")
    project_path = "org:acme/project:app"
    assert usage(rig, filters=UsageFilters(scope=project_path)).total.calls == 4
    # A prefix of a name is not a parent: "service:ap" must not match "service:api".
    with pytest.raises(NotFoundError):
        usage(rig, filters=UsageFilters(scope=f"{project_path}/service:ap"))


def test_limit_offset_and_has_more(seeded: Seeded) -> None:
    page = usage(seeded.rig, group_by=("model", "day"), limit=2)
    assert len(page.rows) == 2 and page.has_more
    rest = usage(seeded.rig, group_by=("model", "day"), limit=2, offset=2)
    assert len(rest.rows) == 1 and not rest.has_more
    assert page.rows[0].cost_nano >= page.rows[1].cost_nano  # biggest spenders first


# --- totals equal the ledger -----------------------------------------------------------------


def test_usage_totals_by_charged_equal_the_ledger_spent(seeded: Seeded) -> None:
    rig = seeded.rig
    spent = sum(rig.totals(w).spent_nano for w in (seeded.api, seeded.web))
    assert spent == E("7.5")
    for dims in ((), ("day",), ("key", "model"), ("service",)):
        result = usage(rig, group_by=dims)
        assert result.total.cost_nano == spent
        assert sum(r.cost_nano for r in result.rows) == spent  # no tag: rows add up
    one_key = usage(rig, filters=UsageFilters(key_id="key-a")).total.cost_nano
    assert one_key == rig.totals(seeded.api).spent_nano


# --- injection -------------------------------------------------------------------------------


def test_a_hostile_filter_or_label_is_inert(seeded: Seeded) -> None:
    rig = seeded.rig
    evil = "x' OR '1'='1'; DROP TABLE calls; --"
    for filters in (
        UsageFilters(model=evil),
        UsageFilters(key_id=evil),
        UsageFilters(user=evil),
        UsageFilters(tag=f"a={evil}"),
        UsageFilters(provider=evil, session_id=evil),
    ):
        assert usage(rig, group_by=("model",), filters=filters).rows == ()
    with pytest.raises(NotFoundError):
        usage(rig, filters=UsageFilters(scope=evil))
    call(rig, seeded.api, "evil", D1 + 5_000, "0.10", user=evil, labels={evil: evil})
    by_user = usage(rig, group_by=("user", "tag"), filters=UsageFilters(user=evil))
    assert {r.group["user"] for r in by_user.rows} == {evil}  # stored and returned verbatim
    assert usage(rig).total.calls == 5  # the table survived


# --- retention -------------------------------------------------------------------------------


def test_retention_boundary_is_a_whole_utc_midnight() -> None:
    now = D0 + 10 * DAY_MS + 12_345
    assert retention_boundary(now, 3) == D0 + 7 * DAY_MS
    assert retention_boundary(D0 + 3 * DAY_MS, 3) == D0  # exactly on a midnight: that day stays
    with pytest.raises(ValueError, match="whole number of days"):
        retention_boundary(now, 0)


def seed_days(rig: Rig, days: int) -> World:
    w, _ = make_tree(rig)
    for day in range(days):
        call(rig, w, f"d{day}-a", D0 + day * DAY_MS, "1.00", usage="1.00")  # first ms of the day
        call(rig, w, f"d{day}-z", D0 + (day + 1) * DAY_MS - 1, "1.00", usage="1.00")  # last ms
    return w


def test_retention_deletes_whole_days_and_usage_refuses_an_older_since(rig: Rig) -> None:
    w = seed_days(rig, 6)  # days 0..5, two calls each
    now = D0 + 5 * DAY_MS + 3_600_000
    outcome = rig.run(rig.ledger.delete_calls_older_than(3, now, 3))  # batches of 3 rows
    assert outcome.boundary_ms == D0 + 2 * DAY_MS
    assert outcome.calls == 4  # days 0 and 1, both their first and last millisecond
    assert outcome.reservations == 4
    assert rig.run(rig.ledger.get_calls("d1-z")) == ()
    assert len(rig.run(rig.ledger.get_calls("d2-a"))) == 1  # the boundary midnight stays
    result = usage(rig, group_by=("day",))
    assert result.since == outcome.boundary_ms  # the window is reported, not hidden
    assert result.total.calls == 8
    with pytest.raises(UsageRangeError) as raised:
        usage(rig, since=outcome.boundary_ms - 1)
    assert raised.value.boundary_ms == outcome.boundary_ms
    assert usage(rig, since=outcome.boundary_ms).total.calls == 8
    # Money state is untouched: periods are never deleted, so spent still covers deleted days.
    assert rig.totals(w).spent_nano == E("12")
    again = rig.run(rig.ledger.delete_calls_older_than(3, now))
    assert (again.calls, again.reservations) == (0, 0)  # idempotent


def test_usage_refuses_a_since_past_the_configured_window_before_any_deletion(rig: Rig) -> None:
    seed_days(rig, 2)
    now = D0 + 10 * DAY_MS
    query = UsageQuery(since=D0)
    with pytest.raises(UsageRangeError):
        rig.run(rig.ledger.usage(query, now=now, retention_days=3))
    assert rig.run(rig.ledger.usage(query, now=now)).total.calls == 4  # no window configured


def test_a_shorter_retention_later_cannot_move_the_boundary_back(rig: Rig) -> None:
    seed_days(rig, 2)
    first = rig.run(rig.ledger.delete_calls_older_than(1, D0 + 5 * DAY_MS))
    second = rig.run(rig.ledger.delete_calls_older_than(30, D0 + 5 * DAY_MS))
    assert second.boundary_ms < first.boundary_ms
    with pytest.raises(UsageRangeError) as raised:
        usage(rig, since=second.boundary_ms)
    assert raised.value.boundary_ms == first.boundary_ms


def test_retention_deletes_closed_sessions_with_their_budgets_and_keeps_open_ones(
    rig: Rig,
) -> None:
    defaults = SessionDefaults(budget_nano=E("5"), max_steps=1)
    w = rig.world("1000", session_defaults=defaults)
    call(rig, w, "old", D0, "1.00", usage="1.00", session="closed-one")
    refused = rig.hold(w, "again", "1", D0 + 1_000, session="closed-one")
    assert not hasattr(refused, "session_scope_id")  # the step limit closed it
    open_held = rig.hold_ok(w, "live", "1", D0 + 6 * DAY_MS, session="open-one")
    closed = next(s for s in _session_ids(rig, w) if s != open_held.session_scope_id)
    result = rig.run(rig.ledger.delete_calls_older_than(3, D0 + 7 * DAY_MS))
    assert result.sessions == 1
    assert rig.run(rig.ledger.get_scope(closed)) is None
    assert rig.run(rig.ledger.get_scope(open_held.session_scope_id or "")) is not None
    assert rig.run(rig.ledger.get_calls("old")) == ()
    assert rig.run(rig.ledger.get_reservation("old")) is None
    pending = rig.run(rig.ledger.get_reservation("live"))
    assert pending is not None  # a pending hold is never deleted


def _session_ids(rig: Rig, w: World) -> list[str]:
    """Session scope ids of a world: found through the held and refused requests' reservations."""
    ids = []
    for rid in ("old", "live"):
        row = rig.run(rig.ledger.get_reservation(rid))
        if row is not None and row.session_id is not None:
            ids.append(row.session_id)
    return ids


# --- performance -----------------------------------------------------------------------------

ROWS = 100_000
_INSERT = """INSERT INTO calls (request_id, attempt, key_id, scope_id, session_id, user_label,
  labels, endpoint, provider, deployment, model, price_id, catalog_sha, tokens_in, tokens_out,
  native_currency, native_cost_nano, fx_rate_nano, charged_nano, usage_cost_nano, outcome,
  started_at, finished_at) VALUES (?, 1, ?, ?, NULL, ?, ?, '/v1/chat', ?, ?, ?, 'p', 'sha', 10, 5,
  'EUR', ?, 1000000000, ?, ?, 'ok', ?, ?)"""


@pytest.mark.slow
def test_a_seven_day_service_and_model_report_over_100k_calls_is_fast(
    tmp_path: Path, new_id: object
) -> None:
    rig = Rig("sqlite", str(tmp_path / "ledger.db"), new_id)  # type: ignore[arg-type]
    try:
        api, web = make_tree(rig)
        models = [f"model-{n}" for n in range(8)]
        rows = [
            (
                f"r{n}",
                "key-a" if n % 2 else "key-b",
                (api if n % 2 else web).scope.id,
                f"user-{n % 50}",
                '{"team": "x"}',
                f"p{n % 3}",
                f"dep-{n % 3}",
                models[n // 2 % 8],  # independent of the service
                1_000 + n,
                1_000 + n,
                1_000 + n,
                D0 + (n % 14) * DAY_MS + n,  # started
                D0 + (n % 14) * DAY_MS + n + 50,  # finished: spread over 14 days
            )
            for n in range(ROWS)
        ]
        with closing(sqlite3.connect(tmp_path / "ledger.db")) as conn, conn:
            conn.executemany(_INSERT, rows)
        since = D0 + 7 * DAY_MS
        query = UsageQuery(group_by=("service", "model"), since=since, until=since + 7 * DAY_MS)
        now = since + 7 * DAY_MS
        rig.run(rig.ledger.usage(query, now=now))  # warm the page cache like a long-lived server
        started = time.perf_counter()
        result = rig.run(rig.ledger.usage(query, now=now))
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(f"usage 7d service+model over {ROWS} calls: {elapsed_ms:.1f} ms")
        assert result.total.calls == sum(1 for n in range(ROWS) if n % 14 >= 7)
        assert len(result.rows) == 16
        assert elapsed_ms < 200
    finally:
        rig.close()
