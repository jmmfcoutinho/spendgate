"""Ledger contract: budget alerts come back from `hold` and `settle` once per transition.

The pipeline logs exactly what these return (operations.md, "Budget warnings"), so how often each
appears is a ledger property: once per period for the soft threshold (soft_alerted_at), once per
crossing for a cap breach, once per review flag (`review_required_at`) for a block.
"""

from spendgate.core.budgets import AlertKind, BudgetAlert, Period
from spendgate.ledger.port import Held
from tests.contract.rig import T0, E, Rig, World

DAY_MS = 86_400_000


def kinds(alerts: tuple[BudgetAlert, ...]) -> list[AlertKind]:
    return [a.kind for a in alerts]


def spend(rig: Rig, w: World, amount: str, rid: str, now: int = T0) -> None:
    rig.hold_ok(w, rid, amount, now)
    rig.settle(rid, amount, now)


def test_soft_threshold_crossing_is_reported_once_with_the_budget_facts(rig: Rig) -> None:
    w = rig.world("10")  # soft_pct 80
    assert rig.hold_ok(w, "A", "7").alerts == ()  # 70 %: below the threshold
    crossed = rig.hold_ok(w, "B", "1")  # 7 + 1 = 80 %: crosses
    (alert,) = crossed.alerts
    assert alert == BudgetAlert(
        AlertKind.SOFT_THRESHOLD, w.budget.id, w.scope.path, Period.NONE, 0, E("8"), E("10")
    )
    assert rig.hold_ok(w, "C", "1").alerts == ()  # still above: no repeat
    rig.settle("A", "7")
    rig.settle("B", "1")
    rig.settle("C", "1")
    assert rig.hold_ok(w, "D", "0.5").alerts == ()  # after settling: still none


def test_a_refused_hold_reports_no_alert_and_does_not_use_up_the_soft_one(rig: Rig) -> None:
    w = rig.world("10")
    assert not isinstance(rig.hold(w, "A", "11"), Held)  # refused: rolled back
    assert kinds(rig.hold_ok(w, "B", "9").alerts) == [AlertKind.SOFT_THRESHOLD]


def test_soft_threshold_is_reported_once_per_period(rig: Rig) -> None:
    w = rig.world("10", period=Period.DAY)
    day2 = T0 + DAY_MS
    assert kinds(rig.hold_ok(w, "A", "9").alerts) == [AlertKind.SOFT_THRESHOLD]
    assert rig.hold_ok(w, "B", "0.5").alerts == ()
    (alert,) = rig.hold_ok(w, "C", "9", day2).alerts  # a new period starts with a fresh row
    assert alert.kind is AlertKind.SOFT_THRESHOLD and alert.period is Period.DAY
    assert alert.period_start > T0 and alert.period_start <= day2
    assert rig.hold_ok(w, "D", "0.5", day2).alerts == ()


def test_a_breach_overrun_reports_cap_breached_and_blocked_once(rig: Rig) -> None:
    w = rig.world("10")
    spend(rig, w, "9", "seed")
    rig.hold_ok(w, "A", "1")
    done = rig.settle("A", "1.80")
    assert kinds(done.alerts) == [AlertKind.CAP_BREACHED, AlertKind.BLOCKED]
    assert {(a.budget_id, a.scope_path, a.spent_nano, a.cap_nano) for a in done.alerts} == {
        (w.budget.id, w.scope.path, E("10.80"), E("10"))
    }
    assert done.review_flagged == (w.budget.id,)
    again = rig.settle("A", "1.80")  # a repeat of the same settle changes nothing
    assert (again.applied, again.alerts) == (False, ())


def test_an_overrun_inside_the_cap_reports_nothing(rig: Rig) -> None:
    w = rig.world("10")
    spend(rig, w, "5", "seed")
    rig.hold_ok(w, "A", "1")
    assert rig.settle("A", "1.50").alerts == ()
    rig.hold_ok(w, "B", "1")
    assert rig.release("B").alerts == ()


def test_cap_breached_is_reported_only_by_the_settle_that_crossed_the_cap(rig: Rig) -> None:
    w = rig.world("10", strict_overrun=False)  # never blocked: only the breach is reported
    spend(rig, w, "7", "seed")
    rig.hold_ok(w, "A", "1")
    rig.hold_ok(w, "B", "1")
    first = rig.settle("A", "2.50")  # 7 + 2.50 + 1 held = 10.50 > 10: crosses
    assert kinds(first.alerts) == [AlertKind.CAP_BREACHED]
    assert first.alerts[0].spent_nano == E("10.50")
    assert rig.settle("B", "2").alerts == ()  # already above the cap: no second breach


def test_a_block_is_reported_again_only_after_an_ack_and_a_new_breach(rig: Rig) -> None:
    w = rig.world("10")
    spend(rig, w, "9", "seed")
    rig.hold_ok(w, "A", "0.5")
    rig.hold_ok(w, "B", "0.5")
    assert kinds(rig.settle("A", "1.50").alerts) == [AlertKind.CAP_BREACHED, AlertKind.BLOCKED]
    assert rig.settle("B", "1").alerts == ()  # still above the cap and flagged: nothing new
    rig.run(rig.ledger.ack_overrun(w.budget.id, actor="admin", reason="checked", now=T0))
    rig.run(rig.ledger.update_budget(w.budget.id, actor="admin", now=T0, cap_nano=E("20")))
    rig.hold_ok(w, "C", "8")  # spent 11.50 + 8 = 19.50, within the raised cap
    again = rig.settle("C", "9")  # 20.50 > 20: a new crossing of the new cap
    assert kinds(again.alerts) == [AlertKind.CAP_BREACHED, AlertKind.BLOCKED]


def test_a_late_settle_that_breaches_the_cap_reports_it(rig: Rig) -> None:
    w = rig.world("10")
    spend(rig, w, "9", "seed")
    rig.hold_ok(w, "A", "1")
    assert rig.sweep(T0 + 60_100) == ("A",)  # the sweeper books the hold: 10, at the cap
    late = rig.settle("A", "1.80", T0 + 61_000, usage="1.80")
    assert late.late and kinds(late.alerts) == [AlertKind.CAP_BREACHED, AlertKind.BLOCKED]
    assert late.alerts[0].spent_nano == E("10.80")
    assert rig.settle("A", "1.80", T0 + 62_000, usage="1.80").alerts == ()


def test_an_ancestor_budget_alert_carries_its_own_scope_path(rig: Rig) -> None:
    w = rig.world("100", org_cap="10")
    (org_budget,) = w.extra
    (alert,) = rig.hold_ok(w, "A", "9").alerts  # 9 % of the user budget, 90 % of the org one
    assert (alert.budget_id, alert.scope_path) == (org_budget.id, w.parent.path)
    breached = rig.settle("A", "10.50").alerts  # 10.50 > the org cap, far inside the user one
    assert kinds(breached) == [AlertKind.CAP_BREACHED, AlertKind.BLOCKED]
    assert {(a.budget_id, a.scope_path) for a in breached} == {(org_budget.id, w.parent.path)}


def test_cap_breached_is_reported_once_per_crossing_after_a_dip_below_the_cap(rig: Rig) -> None:
    w = rig.world("10", strict_overrun=False)
    spend(rig, w, "7", "seed")
    for rid in "ABC":
        rig.hold_ok(w, rid, "1")  # spent 7 + held 3 = 10, at the cap
    assert kinds(rig.settle("A", "2").alerts) == [AlertKind.CAP_BREACHED]  # 9 + 2 = 11: crosses
    assert rig.settle("B", "0").alerts == ()  # 9 + 1 = 10: back within the cap, no overrun
    again = rig.settle("C", "2")  # 11 + 0 = 11: a second crossing in the same period
    assert kinds(again.alerts) == [AlertKind.CAP_BREACHED]
    assert again.alerts[0].spent_nano == E("11")


def test_a_session_budget_alert_names_the_key_scope_never_the_session_or_key_id(
    rig: Rig,
) -> None:
    from spendgate.core.sessions import SessionDefaults  # noqa: PLC0415

    w = rig.world("100", session_defaults=SessionDefaults(budget_nano=E("5")))
    session = "caller-chosen-session-zz9"
    soft = rig.hold_ok(w, "A", "4.5", session=session).alerts  # 90 % of the session budget
    breach = rig.settle("A", "6").alerts  # 6 > 5: crosses the session cap, and blocks it
    assert kinds(soft) == [AlertKind.SOFT_THRESHOLD]
    assert kinds(breach) == [AlertKind.CAP_BREACHED, AlertKind.BLOCKED]
    for alert in (*soft, *breach):
        assert alert.session and alert.scope_path == w.scope.path
        assert session not in alert.scope_path and w.key.id not in alert.scope_path
