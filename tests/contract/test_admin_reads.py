"""The ledger reads and changes added for the admin API, on every adapter."""

import time

import pytest

from spendgate.core.budgets import ScopeKind
from spendgate.core.outcomes import Outcome
from spendgate.core.policy import KillSwitch, KillTarget
from spendgate.ledger.admin_port import AdminLedger, CallCursor, CallFilters
from spendgate.ledger.key_types import KeySpec
from spendgate.ledger.port import ConflictError, NotFoundError
from tests.contract.rig import T0, E, Rig, attempt


def led(rig: Rig) -> AdminLedger:
    """The rig's ledger: every adapter implements the admin port."""
    return rig.ledger


PEPPER = b"test-pepper-0123456789abcdef"
DAY = 86_400_000


def test_database_clock_is_close_to_this_hosts_clock(rig: Rig) -> None:
    # SQLite answers with this host's clock by definition; Postgres with its server's (a local
    # server here), so a generous bound only catches a unit mistake (seconds, microseconds).
    now = rig.run(led(rig).db_now_ms())
    assert abs(now - time.time() * 1000) < 60_000


def test_global_kill_switch_probe_follows_engage_and_lift(rig: Rig) -> None:
    w = rig.world("10")
    assert rig.run(led(rig).global_kill_engaged()) is False
    rig.run(
        led(rig).engage_kill_switch(
            KillSwitch(KillTarget.SCOPE, w.scope.id), actor="a", reason="r", now=T0
        )
    )
    assert rig.run(led(rig).global_kill_engaged()) is False  # only a global switch counts
    sid = rig.run(
        led(rig).engage_kill_switch(KillSwitch(KillTarget.GLOBAL), actor="a", reason="r", now=T0)
    )
    assert rig.run(led(rig).global_kill_engaged()) is True
    rig.run(led(rig).lift_kill_switch(sid, actor="a", now=T0 + 1))
    assert rig.run(led(rig).global_kill_engaged()) is False


def test_kill_switch_list_shows_active_switches_with_who_and_why(rig: Rig) -> None:
    w = rig.world("10")
    keep = rig.run(
        led(rig).engage_kill_switch(
            KillSwitch(KillTarget.MODEL, "openai/x"), actor="ops", reason="cost", now=T0
        )
    )
    lifted = rig.run(
        led(rig).engage_kill_switch(
            KillSwitch(KillTarget.SCOPE, w.scope.id), actor="ops", reason="r", now=T0 + 1
        )
    )
    rig.run(led(rig).lift_kill_switch(lifted, actor="ops", now=T0 + 2))
    [row] = rig.run(led(rig).list_kill_switches())
    assert (row.id, row.reason, row.created_by, row.created_at) == (keep, "cost", "ops", T0)
    assert row.switch == KillSwitch(KillTarget.MODEL, "openai/x") and row.cleared_at is None


def test_pending_reservations_count_open_holds_only(rig: Rig) -> None:
    w = rig.world("10")
    assert rig.run(led(rig).pending_reservations()) == 0
    rig.hold_ok(w, "A", "1")
    rig.hold_ok(w, "B", "1")
    rig.settle("A", "1")
    assert rig.run(led(rig).pending_reservations()) == 1


def test_find_scope_matches_the_path_only_and_misses_unknown_paths(rig: Rig) -> None:
    w = rig.world("10")
    found = rig.run(rig.ledger.find_scope("org:acme/user:u1"))
    assert found is not None and found.id == w.scope.id and found.created_at == T0
    assert rig.run(rig.ledger.find_scope("org:acme")) == w.parent
    assert rig.run(rig.ledger.find_scope(w.scope.id)) is None  # an id is not a path
    assert rig.run(rig.ledger.find_scope("org:nope")) is None


def test_add_key_record_is_idempotent(rig: Rig) -> None:
    w = rig.world("10")
    rig.run(rig.ledger.add_key_record("local-key", w.scope.id, now=T0))
    rig.run(rig.ledger.add_key_record("local-key", w.scope.id, now=T0))  # a racing starter
    assert rig.run(rig.ledger.get_key("local-key")) is not None


def test_scopes_resolve_by_id_or_path_and_list_in_pages(rig: Rig) -> None:
    w = rig.world("10")
    by_id = rig.run(led(rig).resolve_scope(w.scope.id))
    by_path = rig.run(led(rig).resolve_scope("org:acme/user:u1"))
    assert by_id is not None and by_id == by_path and by_id.created_at == T0
    assert rig.run(led(rig).resolve_scope("org:nope")) is None
    everything = rig.run(led(rig).list_scopes(kind=None, parent_id=None, after="", limit=10))
    assert {s.path for s in everything} == {"org:acme", "org:acme/user:u1"}
    page = rig.run(led(rig).list_scopes(kind=None, parent_id=None, after="", limit=1))
    rest = rig.run(led(rig).list_scopes(kind=None, parent_id=None, after=page[0].id, limit=10))
    assert [s.id for s in page + rest] == sorted(s.id for s in everything)
    users = rig.run(
        led(rig).list_scopes(kind=ScopeKind.USER, parent_id=w.parent.id, after="", limit=10)
    )
    assert [s.path for s in users] == ["org:acme/user:u1"]


def test_scope_create_audits_refuses_a_duplicate_and_keeps_a_display_name(rig: Rig) -> None:
    org = rig.run(
        led(rig).create_scope(ScopeKind.ORG, "acme", None, now=T0, actor="adm", display_name="Acme")
    )
    assert org.display_name == "Acme" and org.created_at == T0
    with pytest.raises(ConflictError):
        rig.run(led(rig).create_scope(ScopeKind.ORG, "acme", None, now=T0))
    with pytest.raises(ValueError, match="session"):
        rig.run(led(rig).create_scope(ScopeKind.SESSION, "s", org.id, now=T0))
    renamed = rig.run(led(rig).update_scope(org.id, display_name="ACME", actor="adm2", now=T0 + 5))
    assert renamed.display_name == "ACME"
    assert rig.run(led(rig).resolve_scope(org.id)) == renamed
    entries = rig.run(led(rig).list_audit(since=0, until=None, actor=None, before=None, limit=10))
    assert [(e.action, e.actor) for e in entries] == [
        ("scope.update", "adm2"),
        ("scope.create", "adm"),
    ]
    with pytest.raises(NotFoundError):
        rig.run(led(rig).update_scope("nope", display_name="x", actor="a", now=T0))


def test_budget_views_carry_totals_note_and_disabled_state(rig: Rig) -> None:
    w = rig.world("10", org_cap="50")
    rig.hold_ok(w, "A", "2")
    rig.settle("A", "1.5")
    rig.hold_ok(w, "B", "1")
    view = rig.run(led(rig).get_budget(w.budget.id, T0))
    assert view is not None and not view.disabled and view.note is None
    t = view.totals
    assert (t.effective_cap_nano, t.spent_nano, t.held_nano) == (E("10"), E("1.5"), E("1"))
    scoped = rig.run(led(rig).list_budgets(w.scope.id, T0))
    assert [v.budget.id for v in scoped] == [w.budget.id]
    every = rig.run(led(rig).list_budgets(None, T0))
    assert {v.budget.id for v in every} == {w.budget.id, w.extra[0].id}
    rig.run(led(rig).update_budget(w.budget.id, actor="a", now=T0, note="why"))
    noted = rig.run(led(rig).get_budget(w.budget.id, T0))
    assert noted is not None and noted.note == "why"
    rig.disable_budget(w.budget.id)
    gone = rig.run(led(rig).get_budget(w.budget.id, T0))
    assert gone is not None and gone.disabled
    assert w.budget.id not in {v.budget.id for v in rig.run(led(rig).list_budgets(None, T0))}
    assert rig.run(led(rig).get_budget("nope", T0)) is None


def test_a_budget_may_be_created_under_a_chosen_id_once(rig: Rig) -> None:
    w = rig.world("10")
    made = rig.run(
        led(rig).create_budget(w.scope.id, w.budget.period, E("3"), now=T0, budget_id="bootstrap-x")
    )
    assert made.id == "bootstrap-x"
    with pytest.raises(ConflictError):
        rig.run(
            led(rig).create_budget(
                w.scope.id, w.budget.period, E("3"), now=T0, budget_id="bootstrap-x"
            )
        )


def test_key_expiry_can_be_set_and_cleared_with_an_audit_row(rig: Rig) -> None:
    w = rig.world("10")
    made = rig.run(
        led(rig).create_key(KeySpec(w.scope.id, "k"), pepper=PEPPER, actor="adm", now=T0)
    )
    rec = rig.run(led(rig).set_key_expiry(made.record.id, T0 + DAY, actor="adm", now=T0 + 1))
    assert rec.expires_at == T0 + DAY
    stored = rig.run(led(rig).get_key(made.record.id))
    assert stored is not None and stored.expires_at == T0 + DAY
    cleared = rig.run(led(rig).set_key_expiry(made.record.id, None, actor="adm", now=T0 + 2))
    assert cleared.expires_at is None
    entries = rig.run(led(rig).list_audit(since=0, until=None, actor="adm", before=None, limit=10))
    assert [e.action for e in entries][:2] == ["key.update", "key.update"]
    with pytest.raises(NotFoundError):
        rig.run(led(rig).set_key_expiry("nope", None, actor="a", now=T0))


def test_audit_lists_newest_first_filters_and_pages(rig: Rig) -> None:
    w = rig.world("10")
    for n in range(3):
        rig.run(
            led(rig).update_budget(
                w.budget.id, actor=f"a{n % 2}", now=T0 + 10 * (n + 1), cap_nano=E("5")
            )
        )
    full = rig.run(led(rig).list_audit(since=0, until=None, actor=None, before=None, limit=50))
    assert [e.at for e in full] == sorted((e.at for e in full), reverse=True)
    assert full[0].action == "budget.set_cap" and full[0].target_id == w.budget.id
    assert full[0].before is not None and full[0].after is not None
    mine = rig.run(led(rig).list_audit(since=0, until=None, actor="a1", before=None, limit=50))
    assert {e.actor for e in mine} == {"a1"} and len(mine) == 1
    window = rig.run(
        led(rig).list_audit(since=T0 + 15, until=T0 + 30, actor=None, before=None, limit=50)
    )
    assert [e.at for e in window] == [T0 + 20]
    first = rig.run(led(rig).list_audit(since=0, until=None, actor=None, before=None, limit=2))
    cursor = (first[-1].at, first[-1].id)
    more = rig.run(led(rig).list_audit(since=0, until=None, actor=None, before=cursor, limit=50))
    assert [e.id for e in first + more] == [e.id for e in full]


def test_calls_list_newest_first_filter_page_and_get_returns_the_last_attempt(rig: Rig) -> None:
    w = rig.world("10")
    for n, rid in enumerate(("A", "B", "C")):
        rig.hold_ok(w, rid, "1", T0 + n * 1_000)
        rig.settle(rid, "0.5", T0 + n * 1_000 + 500)
    rig.hold_ok(w, "D", "1", T0 + 5_000)
    failover = (
        attempt(1, Outcome.UPSTREAM_5XX, 0, T0 + 5_100, True, provider="p1"),
        attempt(2, Outcome.OK, E("0.5"), T0 + 5_500, usage_cost_nano=E("0.5"), provider="p2"),
    )
    rig.settle("D", "0.5", T0 + 5_500, attempts=failover)
    no_filter = CallFilters()
    rows = rig.run(led(rig).list_calls(no_filter, before=None, limit=50))
    assert [r["request_id"] for r in rows] == ["D", "D", "C", "B", "A"]
    assert rows[0]["attempt"] == 2 and rows[0]["streamed"] == 0
    page = rig.run(led(rig).list_calls(no_filter, before=None, limit=2))
    last = page[-1]
    cursor = CallCursor(
        int(str(last["finished_at"])), str(last["request_id"]), int(str(last["attempt"]))
    )
    rest = rig.run(led(rig).list_calls(no_filter, before=cursor, limit=50))
    assert [(r["request_id"], r["attempt"]) for r in page + rest] == [
        (r["request_id"], r["attempt"]) for r in rows
    ]
    by_outcome = rig.run(
        led(rig).list_calls(CallFilters(outcome="upstream_5xx"), before=None, limit=9)
    )
    assert [(r["request_id"], r["attempt"]) for r in by_outcome] == [("D", 1)]
    scoped = CallFilters(scope_id=w.scope.id, key_id=w.key.id, since=T0 + 1_000, until=T0 + 2_600)
    assert [
        r["request_id"] for r in rig.run(led(rig).list_calls(scoped, before=None, limit=9))
    ] == ["C", "B"]
    elsewhere = CallFilters(scope_id=w.parent.id)
    assert rig.run(led(rig).list_calls(elsewhere, before=None, limit=9)) == ()
    final = rig.run(led(rig).get_call("D"))
    assert final is not None and final["attempt"] == 2 and final["provider"] == "p2"
    assert rig.run(led(rig).get_call("nope")) is None
