"""Ledger contract: sessions, kill switches, filters and other admission rules."""

import pytest

from spendgate.core.budgets import Period, ScopeKind
from spendgate.core.errors import ErrorCode
from spendgate.core.policy import KeyPolicy, KillSwitch, KillTarget, Limits, Plan, PlannedBudget
from spendgate.core.sessions import SessionDefaults
from spendgate.ledger.port import Held, HoldRequest, NotFoundError, Refused, ScopeRow
from tests.contract.rig import SECOND, T0, E, Rig, World
from tests.contract.test_ledger import figures, refusal_of

HOUR = 3_600 * SECOND


def limit_of(result: object) -> str:
    assert isinstance(result, Refused), result
    assert result.refusal.code is ErrorCode.SESSION_LIMIT_REACHED
    return str(result.refusal.details["limit"])


def scope_of(rig: Rig, held: Held) -> ScopeRow:
    assert held.session_scope_id is not None
    row = rig.run(rig.ledger.get_scope(held.session_scope_id))
    assert row is not None
    return row


# --- scenario 9 and step limits --------------------------------------------------------------


def test_scenario_09_session_step_limit_with_a_call_in_flight(rig: Rig) -> None:
    w = rig.world("100", session_defaults=SessionDefaults(budget_nano=E("5"), max_steps=2))
    a = rig.hold_ok(w, "A", "1", session="s1")
    rig.hold_ok(w, "B", "1", session="s1")
    assert limit_of(rig.hold(w, "C", "1", session="s1")) == "steps"  # nothing held for C
    assert scope_of(rig, a).closed_at is None  # A and B are in flight: a release may refund a step
    session_budget = rig.run(rig.ledger.totals(a.session_scope_id or "", T0))[0]
    assert session_budget.held_nano == E("2")  # mid-run H 2.00
    rig.release("B")  # every candidate answered 429
    assert scope_of(rig, a).step_count == 1
    c = rig.hold_ok(w, "C2", "1", session="s1")  # C retried as a new request
    assert scope_of(rig, c).step_count == 2
    rig.settle("A", "0.70")
    rig.settle("C2", "0.20")
    final = rig.run(rig.ledger.totals(a.session_scope_id or "", T0))[0]
    assert figures(final) == (E("0.90"), 0, 0, 0)


def test_t_sess_07_a_session_at_its_step_limit_stays_closed_with_the_stored_reason(
    rig: Rig,
) -> None:
    w = rig.world("100", session_defaults=SessionDefaults(max_steps=1, idle_seconds=600))
    held = rig.hold_ok(w, "A", "1", session="s1")
    rig.settle("A", "0.5")
    assert limit_of(rig.hold(w, "B", "1", session="s1", now=T0 + SECOND)) == "steps"
    closed = scope_of(rig, held)
    assert (closed.closed_at, closed.closed_reason) == (T0 + SECOND, "steps")
    # Long after the idle limit it is still the same closed session, never a restart.
    assert limit_of(rig.hold(w, "C", "1", session="s1", now=T0 + 99 * HOUR)) == "steps"


def test_t_sess_07_two_keys_on_one_scope_never_share_a_session(rig: Rig) -> None:
    defaults = SessionDefaults(max_steps=1)
    w = rig.world("100", session_defaults=defaults)
    other = rig.add_key(w, "key-2", defaults)
    first = rig.hold_ok(w, "A", "1", session="shared")
    second = rig.hold_ok(w, "B", "1", session="shared", key=other)
    assert first.session_scope_id != second.session_scope_id
    assert limit_of(rig.hold(w, "C", "1", session="shared")) == "steps"


def test_session_time_limit_closes_the_session(rig: Rig) -> None:
    w = rig.world("100", session_defaults=SessionDefaults(max_seconds=10))
    held = rig.hold_ok(w, "A", "1", session="s1")
    rig.settle("A", "0.1")
    assert isinstance(rig.hold(w, "B", "1", session="s1", now=T0 + 10 * SECOND), Held)
    assert limit_of(rig.hold(w, "C", "1", session="s1", now=T0 + 11 * SECOND)) == "time"
    assert scope_of(rig, held).closed_reason == "time"  # time is final even with a call in flight


# --- session budget --------------------------------------------------------------------


def test_l_a_session_own_budget_refusal_is_a_session_limit_and_closes_it(rig: Rig) -> None:
    w = rig.world("100", session_defaults=SessionDefaults(budget_nano=E("1")))
    held = rig.hold_ok(w, "A", "0.6", session="s1")
    rig.settle("A", "0.6")
    assert limit_of(rig.hold(w, "B", "0.6", session="s1")) == "budget"
    assert scope_of(rig, held).closed_reason == "budget"
    assert limit_of(rig.hold(w, "C", "0.01", session="s1")) == "budget"  # closed means closed
    assert figures(rig.totals(w)) == (E("0.6"), 0, 0, 0)  # the parent budget is untouched


def test_session_budget_refusal_with_a_call_in_flight_does_not_close_yet(rig: Rig) -> None:
    w = rig.world("100", session_defaults=SessionDefaults(budget_nano=E("1")))
    held = rig.hold_ok(w, "A", "0.6", session="s1")
    assert limit_of(rig.hold(w, "B", "0.6", session="s1")) == "budget"
    assert scope_of(rig, held).closed_at is None
    rig.release("A")  # A's hold goes back, so the session has its budget again
    assert isinstance(rig.hold(w, "C", "0.6", session="s1"), Held)


def test_a_refused_new_session_leaves_no_row_and_no_step(rig: Rig) -> None:
    w = rig.world("100", session_defaults=SessionDefaults(budget_nano=E("1"), max_steps=1))
    assert limit_of(rig.hold(w, "A", "5", session="s1")) == "budget"  # session created, then undone
    held = rig.hold_ok(w, "B", "0.5", session="s1")  # not closed: the first attempt left no trace
    assert scope_of(rig, held).step_count == 1


def test_a_parent_budget_refusal_consumes_no_session_step(rig: Rig) -> None:
    w = rig.world("1", session_defaults=SessionDefaults(max_steps=1))
    assert refusal_of(rig.hold(w, "A", "5", session="s1")) is ErrorCode.BUDGET_EXHAUSTED
    held = rig.hold_ok(w, "B", "0.5", session="s1")
    assert scope_of(rig, held).step_count == 1


# --- scenario 14 and new-session rate --------------------------------------------------------


def test_scenario_14_t_sess_04_idle_restart_closes_renames_and_recreates(rig: Rig) -> None:
    w = rig.world("100", session_defaults=SessionDefaults(max_steps=5, idle_seconds=600))
    first: Held | None = None
    for n in range(3):
        held = rig.hold_ok(w, f"r{n}", "1", session="s1", now=T0 + n)
        rig.settle(f"r{n}", "0.1", T0 + n)
        first = first or held
    assert first is not None
    fresh = rig.hold_ok(w, "after", "1", session="s1", now=T0 + 700 * SECOND)
    old, new = scope_of(rig, first), scope_of(rig, fresh)
    assert old.id != new.id and new.step_count == 1 and new.started_at == T0 + 700 * SECOND
    assert old.step_count == 3 and old.closed_at == T0 + 700 * SECOND
    assert old.closed_reason is None  # an idle restart is not a limit
    assert old.name.endswith(f"#{old.started_at}") and old.path.endswith(f"#{old.started_at}")
    assert new.name == "key-1:s1" and new.path.endswith("/session:key-1:s1")
    assert rig.run(rig.ledger.get_calls("r0"))[0].request_id == "r0"  # history keeps its session


def test_t_sess_05_max_new_sessions_per_hour_gates_creation_and_restart(rig: Rig) -> None:
    w = rig.world(
        "100", session_defaults=SessionDefaults(max_new_sessions_per_hour=2, idle_seconds=60)
    )
    rig.hold_ok(w, "a", "0.1", session="s1")
    rig.hold_ok(w, "b", "0.1", session="s2")
    assert limit_of(rig.hold(w, "c", "0.1", session="s3")) == "new_sessions"
    assert isinstance(rig.hold(w, "d", "0.1", session="s1", now=T0 + 30 * SECOND), Held)  # live
    idle = T0 + 100 * SECOND  # s1 is idle: restarting would be a third new session
    assert limit_of(rig.hold(w, "e", "0.1", session="s1", now=idle)) == "new_sessions"
    assert limit_of(rig.hold(w, "f", "0.1", session="s1", now=idle + SECOND)) == "new_sessions"
    assert isinstance(rig.hold(w, "g", "0.1", session="s3", now=T0 + HOUR + SECOND), Held)


def test_a_session_on_a_key_without_defaults_is_a_programming_error(rig: Rig) -> None:
    w = rig.world("100")
    with pytest.raises(ValueError, match="session_defaults"):
        rig.hold(w, "A", "1", session="s1")


# --- kill switches ---------------------------------------------------------------------------


def _engage(rig: Rig, switch: KillSwitch) -> str:
    return rig.run(rig.ledger.engage_kill_switch(switch, actor="admin", reason="test", now=T0))


@pytest.mark.parametrize(
    "target",
    [
        lambda w: KillSwitch(KillTarget.GLOBAL),
        lambda w: KillSwitch(KillTarget.SCOPE, w.scope.id),
        lambda w: KillSwitch(KillTarget.SCOPE, w.parent.id),  # an ancestor kills its descendants
        lambda w: KillSwitch(KillTarget.KEY, "key-1"),
        lambda w: KillSwitch(KillTarget.MODEL, "gpt"),
        lambda w: KillSwitch(KillTarget.PROVIDER, "p1"),
    ],
    ids=["global", "scope", "ancestor-scope", "key", "model", "provider"],
)
def test_t_blk_03_kill_switch_refuses_inside_the_admission_and_lifts(rig: Rig, target) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
    w = rig.world("100")
    switch_id = _engage(rig, target(w))
    assert refusal_of(rig.hold(w, "A", "1")) is ErrorCode.KILL_SWITCH
    assert rig.run(rig.ledger.get_reservation("A")) is None and figures(rig.totals(w)) == (0,) * 4
    rig.run(rig.ledger.lift_kill_switch(switch_id, actor="admin", now=T0))
    assert isinstance(rig.hold(w, "B", "1"), Held)
    rig.run(rig.ledger.lift_kill_switch(switch_id, actor="admin", now=T0))  # lifting twice is fine


def test_a_provider_kill_switch_needs_every_candidate_killed(rig: Rig) -> None:
    w = rig.world("100")
    _engage(rig, KillSwitch(KillTarget.PROVIDER, "p1"))
    assert isinstance(rig.hold(w, "A", "1", providers=("p1", "p2")), Held)  # p2 can still serve
    assert refusal_of(rig.hold(w, "B", "1", providers=("p1",))) is ErrorCode.KILL_SWITCH


def test_lifting_an_unknown_kill_switch_is_not_found(rig: Rig) -> None:
    rig.world("100")
    with pytest.raises(NotFoundError):
        rig.run(rig.ledger.lift_kill_switch("nope", actor="admin", now=T0))


# --- chain, filters, plan, warnings ----------------------------------------------------------


def test_a_model_filtered_budget_only_applies_to_matching_models(rig: Rig) -> None:
    w = rig.world("100")
    narrow = rig.run(
        rig.ledger.create_budget(w.scope.id, Period.NONE, E("1"), now=T0, model_filter="claude*")
    )
    assert isinstance(rig.hold(w, "A", "5", model="gpt"), Held)  # the claude cap is not in play
    assert refusal_of(rig.hold(w, "B", "5", model="claude-x")) is ErrorCode.BUDGET_EXHAUSTED
    assert rig.totals(w, budget=narrow).held_nano == 0


def test_every_budget_on_the_chain_must_pass_and_all_are_held_together(rig: Rig) -> None:
    w = rig.world("10", org_cap="3")
    held = rig.hold_ok(w, "A", "2")
    assert {i.budget_id for i in held.items} == {w.budget.id, w.extra[0].id}
    assert rig.totals(w).held_nano == rig.totals(w, budget=w.extra[0]).held_nano == E("2")
    assert refusal_of(rig.hold(w, "B", "2")) is ErrorCode.BUDGET_EXHAUSTED  # org: 2 + 2 > 3


def test_a_plan_can_add_a_budget_but_never_remove_one(rig: Rig) -> None:
    w = rig.world("100")
    other = rig.run(rig.ledger.create_scope(ScopeKind.ORG, "other", None, now=T0))
    stray = rig.run(rig.ledger.create_budget(other.id, Period.NONE, E("1"), now=T0))
    plan = Plan((PlannedBudget(stray, 0),), Limits(None, None, None), None, None, None)
    request = rig.request(w, "A", "5")
    from dataclasses import replace  # noqa: PLC0415

    refused = rig.run(rig.ledger.hold(replace(request, plan=plan)))
    assert refusal_of(refused) is ErrorCode.BUDGET_EXHAUSTED  # the planned budget applied


def test_the_soft_threshold_warns_once(rig: Rig) -> None:
    w = rig.world("10")
    first = rig.hold_ok(w, "A", "8")  # 80 % of the cap
    assert [x.budget_id for x in first.warnings] == [w.budget.id]
    assert rig.hold_ok(w, "B", "0.5").warnings == ()


def test_a_hold_for_an_unknown_scope_fails_closed(rig: Rig) -> None:
    w = rig.world("10")
    ghost = KeyPolicy("key-1", "no-such-scope")
    with pytest.raises(NotFoundError):
        rig.hold(w, "A", "1", key=ghost)
    with pytest.raises(ValueError, match="negative"):
        rig.run(rig.ledger.hold(_negative(rig, w)))


def _negative(rig: Rig, w: World) -> HoldRequest:
    from dataclasses import replace  # noqa: PLC0415

    return replace(rig.request(w, "neg", "1"), worst_nano=-1)


def test_t_led_01_injection_strings_leave_the_schema_intact(rig: Rig) -> None:
    evil = "x'); DROP TABLE reservations; --"
    w = rig.world("10", session_defaults=SessionDefaults(max_steps=3))
    held = rig.hold_ok(
        w,
        evil,
        "1",
        session=evil,
        labels={evil: evil},
        user_label=evil,
        model="m'; DELETE FROM calls",
    )
    rig.settle(evil, "0.5")
    row = rig.run(rig.ledger.get_reservation(evil))
    assert row is not None and row.state.value == "settled" and held.session_scope_id
    assert figures(rig.totals(w)) == (E("0.5"), 0, 0, 0)
    assert len(rig.run(rig.ledger.get_calls(evil))) == 1
    rig.hold_ok(w, "after", "1")  # tables still exist and work


def test_scopes_must_nest_org_project_service_user(rig: Rig) -> None:
    org = rig.run(rig.ledger.create_scope(ScopeKind.ORG, "o", None, now=T0))
    with pytest.raises(ValueError, match="below its parent"):
        rig.run(rig.ledger.create_scope(ScopeKind.ORG, "o2", org.id, now=T0))
    with pytest.raises(ValueError, match="no parent"):
        rig.run(rig.ledger.create_scope(ScopeKind.USER, "u", None, now=T0))
    with pytest.raises(ValueError, match="session"):
        rig.run(rig.ledger.create_scope(ScopeKind.SESSION, "s", org.id, now=T0))
    with pytest.raises(NotFoundError):
        rig.run(rig.ledger.create_scope(ScopeKind.USER, "u", "ghost", now=T0))


# --- fail closed: no budget, disabled scope (ADR-0003) ---------------------------------------


def _unbudgeted(rig: Rig) -> World:
    parent = rig.run(rig.ledger.create_scope(ScopeKind.ORG, "acme", None, now=T0))
    scope = rig.run(rig.ledger.create_scope(ScopeKind.USER, "u1", parent.id, now=T0))
    rig.run(rig.ledger.add_key_record("key-1", scope.id, now=T0))
    budget = rig.run(rig.ledger.create_budget(parent.id, Period.NONE, E("5"), now=T0))
    return World(scope, budget, KeyPolicy("key-1", scope.id), parent)


def _forbidden_reason(result: object) -> str:
    assert isinstance(result, Refused), result
    assert result.refusal.code is ErrorCode.FORBIDDEN
    return str(result.refusal.details["reason"])


def test_a_key_with_no_budget_on_its_chain_is_refused(rig: Rig) -> None:
    w = _unbudgeted(rig)
    # This key sits on an org scope that has no budget at all.
    lonely = rig.run(rig.ledger.create_scope(ScopeKind.ORG, "lonely", None, now=T0))
    rig.run(rig.ledger.add_key_record("key-2", lonely.id, now=T0))
    result = rig.hold(w, "A", "1", key=KeyPolicy("key-2", lonely.id))
    assert _forbidden_reason(result) == "no_budget"
    assert rig.run(rig.ledger.get_reservation("A")) is None
    assert isinstance(rig.hold(w, "B", "1"), Held)  # the budgeted key is unaffected


def test_a_session_on_a_key_with_no_budget_leaves_no_session_row(rig: Rig) -> None:
    w = _unbudgeted(rig)
    lonely = rig.run(rig.ledger.create_scope(ScopeKind.ORG, "lonely", None, now=T0))
    rig.run(rig.ledger.add_key_record("key-2", lonely.id, now=T0))
    key = KeyPolicy("key-2", lonely.id, session_defaults=SessionDefaults(max_steps=1))
    assert _forbidden_reason(rig.hold(w, "A", "1", key=key, session="s1")) == "no_budget"


# --- M-17: a session budget alone is not a budget (ADR-0030) ---------------------------------


def _lonely_session_key(rig: Rig) -> KeyPolicy:
    """A key on a new org with no budget, whose writer gave every session a large budget."""
    lonely = rig.run(rig.ledger.create_scope(ScopeKind.ORG, "lonely", None, now=T0))
    rig.run(rig.ledger.add_key_record("key-2", lonely.id, now=T0))
    defaults = SessionDefaults(budget_nano=E("1000000"), max_new_sessions_per_hour=None)
    return KeyPolicy("key-2", lonely.id, session_defaults=defaults)


def test_m17_a_session_budget_alone_is_not_a_budget(rig: Rig) -> None:
    w = _unbudgeted(rig)
    key = _lonely_session_key(rig)
    for n, session in enumerate(("s1", "s2", None)):
        result = rig.hold(w, f"A{n}", "1", key=key, session=session)
        assert _forbidden_reason(result) == "no_budget"
        assert rig.run(rig.ledger.get_reservation(f"A{n}")) is None
    # The refused sessions left no scope, so no session budget is left behind either.
    lonely = rig.run(rig.ledger.find_scope("org:lonely"))
    assert lonely is not None
    sessions = rig.ledger.list_scopes(
        kind=ScopeKind.SESSION, parent_id=lonely.id, after="", limit=9
    )
    assert rig.run(sessions) == ()


def test_m17_a_session_budget_still_narrows_the_shared_budget(rig: Rig) -> None:
    w = rig.world("100", session_defaults=SessionDefaults(budget_nano=E("5")))
    assert limit_of(rig.hold(w, "A", "6", session="s1")) == "budget"  # the session is tighter
    held = rig.hold_ok(w, "B", "4", session="s2")
    assert {item.budget_id for item in held.items} >= {w.budget.id}  # held in the shared one too


def test_m17_a_shared_budget_for_another_model_does_not_count(rig: Rig) -> None:
    parent = rig.run(rig.ledger.create_scope(ScopeKind.ORG, "acme", None, now=T0))
    rig.run(rig.ledger.create_budget(parent.id, Period.NONE, E("100"), now=T0, model_filter="x*"))
    rig.run(rig.ledger.add_key_record("key-1", parent.id, now=T0))
    key = KeyPolicy("key-1", parent.id, session_defaults=SessionDefaults(budget_nano=E("50")))
    w = World(parent, rig.run(rig.ledger.list_budgets(parent.id, T0))[0].budget, key, parent)
    assert _forbidden_reason(rig.hold(w, "A", "1", session="s1", model="gpt")) == "no_budget"
    assert isinstance(rig.hold(w, "B", "1", session="s1", model="x1"), Held)


def test_m17_a_key_on_a_session_scope_does_not_count_that_session_budget(rig: Rig) -> None:
    w = rig.world("100", session_defaults=SessionDefaults(budget_nano=E("50")))
    held = rig.hold_ok(w, "A", "1", session="s1")
    rig.settle("A", "1")
    rig.disable_budget(w.budget.id)  # only the session's own budget is left on its chain
    assert held.session_scope_id is not None
    rig.run(rig.ledger.add_key_record("key-s", held.session_scope_id, now=T0))
    on_session = KeyPolicy("key-s", held.session_scope_id)
    assert _forbidden_reason(rig.hold(w, "B", "1", key=on_session)) == "no_budget"
    assert _forbidden_reason(rig.hold(w, "C", "1", session="s1")) == "no_budget"


def test_a_disabled_budget_stops_applying_and_no_budget_left_refuses(rig: Rig) -> None:
    w = rig.world("10")
    assert isinstance(rig.hold(w, "A", "1"), Held)
    rig.disable_budget(w.budget.id)
    assert _forbidden_reason(rig.hold(w, "B", "1")) == "no_budget"


def test_a_hold_in_a_disabled_scope_is_refused_naming_the_scope(rig: Rig) -> None:
    w = rig.world("10")
    rig.run(rig.ledger.disable_scope(w.parent.id, actor="admin", now=T0 + 10))
    assert isinstance(rig.hold(w, "A", "1", T0 + 9), Held)  # not disabled yet
    result = rig.hold(w, "B", "1", T0 + 10)
    assert _forbidden_reason(result) == "scope_disabled"
    assert isinstance(result, Refused) and result.refusal.details["scope"] == w.parent.path
    assert rig.run(rig.ledger.get_reservation("B")) is None
