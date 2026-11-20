"""resolve(): the budget chain, model filters, kill switches, labels and session gating."""

from dataclasses import replace

import pytest

from spendgate.core.budgets import Budget, Period, PeriodState, ScopeKind, check_admission
from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.policy import (
    SESSION_HEADER,
    TAGS_HEADER,
    USER_HEADER,
    KeyPolicy,
    KillSwitch,
    KillTarget,
    Labels,
    Plan,
    check_labels,
    model_allowed,
    resolve,
)
from spendgate.core.sessions import SessionAction, SessionDefaults, SessionState

NOW = 1_800_000_000_000
SCOPES = ["org", "project", "service", "user", "session"]
MODEL = "openai/gpt-5-mini"


def key(**kw: object) -> KeyPolicy:
    return replace(KeyPolicy(id="key1", scope_id="service"), **kw)  # type: ignore[arg-type]


def bud(bid: str, scope: str, **kw: object) -> Budget:
    base = Budget(id=bid, scope_id=scope, period=Period.NONE, cap_nano=1_000)
    return replace(base, **kw)  # type: ignore[arg-type]


def plan(result: Plan | Refusal) -> Plan:
    assert isinstance(result, Plan), result
    return result


def refusal(result: Plan | Refusal) -> Refusal:
    assert isinstance(result, Refusal), result
    return result


def run(k: KeyPolicy | None = None, labels: Labels | None = None, **kw: object) -> Plan | Refusal:
    args: dict[str, object] = {"budgets": [], "chain_scope_ids": set(SCOPES[:4])}
    args.update(kw)
    return resolve(k or key(), labels or Labels(), MODEL, NOW, **args)  # type: ignore[arg-type]


def test_a_chain_of_five_scopes_yields_every_budget_ordered_by_id() -> None:
    budgets = [bud(f"b{i}", s) for i, s in reversed(list(enumerate(SCOPES)))]
    result = plan(run(budgets=budgets, chain_scope_ids=set(SCOPES)))
    assert [p.budget.id for p in result.budgets] == ["b0", "b1", "b2", "b3", "b4"]


def test_a_model_filtered_budget_applies_only_to_matching_models() -> None:
    budgets = [
        bud("a", "org"),
        bud("b", "org", model_filter="openai/*"),
        bud("c", "org", model_filter="anthropic/*"),
    ]
    ids = [p.budget.id for p in plan(run(budgets=budgets)).budgets]
    assert ids == ["a", "b"]


def test_labels_never_add_or_remove_a_budget_the_key_is_not_bound_to() -> None:
    budgets = [bud("mine", "service"), bud("theirs", "other-org"), bud("user-tagged", "user:42")]
    base = plan(run(budgets=budgets))
    labelled = plan(run(labels=Labels(user="user:42", tags=("tenant-other-org",)), budgets=budgets))
    assert [p.budget.id for p in base.budgets] == ["mine"]
    assert [p.budget.id for p in labelled.budgets] == ["mine"]


def test_each_planned_budget_carries_the_period_row_of_now() -> None:
    result = plan(run(budgets=[bud("d", "org", period=Period.DAY), bud("n", "org")]))
    by_id = {p.budget.id: p.period_start for p in result.budgets}
    assert by_id["n"] == 0
    assert by_id["d"] == NOW - NOW % 86_400_000


def test_limits_and_ceiling_come_from_the_key() -> None:
    k = key(rpm=10, tpm=1000, max_concurrent=2, max_output_tokens=512, per_call_ceiling_nano=99)
    result = plan(run(k))
    assert (result.limits.rpm, result.limits.tpm, result.limits.max_concurrent) == (10, 1000, 2)
    assert (result.max_output_tokens, result.per_call_ceiling_nano) == (512, 99)
    assert result.session is None


# --- models ----------------------------------------------------------------------------------


def test_model_not_allowed_is_refused_with_403() -> None:
    r = refusal(run(key(allowed_models=("anthropic/*",))))
    assert r.code is ErrorCode.MODEL_NOT_ALLOWED and r.http_status == 403


def test_model_globs_and_unrestricted_keys() -> None:
    assert model_allowed(key(), MODEL)
    assert model_allowed(key(allowed_models=("x/*", "openai/gpt-*")), MODEL)
    assert not model_allowed(key(allowed_models=()), MODEL)


# --- kill switches ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "switch",
    [
        KillSwitch(KillTarget.GLOBAL),
        KillSwitch(KillTarget.SCOPE, "project"),
        KillSwitch(KillTarget.KEY, "key1"),
        KillSwitch(KillTarget.PROVIDER, "openai"),
        KillSwitch(KillTarget.MODEL, MODEL),
    ],
)
def test_a_kill_switch_on_each_target_refuses(switch: KillSwitch) -> None:
    r = refusal(run(kill_switches=[switch], providers=["openai"]))
    assert r.code is ErrorCode.KILL_SWITCH and r.http_status == 503
    assert switch.target_kind.value in r.message


@pytest.mark.parametrize(
    "switch",
    [
        KillSwitch(KillTarget.SCOPE, "unrelated"),
        KillSwitch(KillTarget.SCOPE, "session"),  # not in this call's chain
        KillSwitch(KillTarget.KEY, "key2"),
        KillSwitch(KillTarget.PROVIDER, "anthropic"),
        KillSwitch(KillTarget.MODEL, "openai/gpt-5"),
    ],
)
def test_a_kill_switch_on_something_else_does_not_refuse(switch: KillSwitch) -> None:
    assert isinstance(run(kill_switches=[switch], providers=["openai"]), Plan)


def test_a_provider_switch_refuses_only_when_every_candidate_is_killed() -> None:
    killed = [KillSwitch(KillTarget.PROVIDER, "openai")]
    assert isinstance(run(kill_switches=killed, providers=["openai", "azure_openai"]), Plan)
    both = [*killed, KillSwitch(KillTarget.PROVIDER, "azure_openai")]
    assert isinstance(run(kill_switches=both, providers=["openai", "azure_openai"]), Refusal)
    assert isinstance(run(kill_switches=killed), Plan)  # no candidates known: nothing to kill


def test_kill_switch_validation() -> None:
    with pytest.raises(ValueError, match="global"):
        KillSwitch(KillTarget.GLOBAL, "x")
    with pytest.raises(ValueError, match="global"):
        KillSwitch(KillTarget.KEY)


# --- labels ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("labels", "param"),
    [
        (Labels(session="has space"), SESSION_HEADER),
        (Labels(session="x" * 129), SESSION_HEADER),
        (Labels(session=""), SESSION_HEADER),
        (Labels(user="bad/user"), USER_HEADER),
        (Labels(tags=("UPPER",)), TAGS_HEADER),
        (Labels(tags=("a" * 33,)), TAGS_HEADER),
        (Labels(tags=tuple(f"t{i}" for i in range(11))), TAGS_HEADER),
    ],
)
def test_invalid_label_values_are_refused_naming_the_header(labels: Labels, param: str) -> None:
    r = check_labels(labels)
    assert r is not None and r.code is ErrorCode.INVALID_REQUEST and r.param == param
    assert refusal(run(labels=labels)).param == param


def test_valid_labels_pass() -> None:
    assert check_labels(Labels("nightly-import_1:a.b", "u-42", ("import", "batch.1"))) is None
    assert check_labels(Labels(tags=tuple(f"t{i}" for i in range(10)))) is None


# --- sessions --------------------------------------------------------------------------------

DEFAULTS = SessionDefaults(budget_nano=500, max_steps=2, max_seconds=60, idle_seconds=30)
SESS_KEY = key(session_defaults=DEFAULTS)
SESS = Labels(session="run-1")


def live(**kw: object) -> SessionState:
    base = SessionState(NOW - 1_000, NOW - 1_000, 0, 2, 60, 30)
    return replace(base, **kw)  # type: ignore[arg-type]


def test_m11_a_session_header_on_a_key_without_defaults_is_invalid_request() -> None:
    r = refusal(run(labels=SESS))
    assert r.code is ErrorCode.INVALID_REQUEST and r.param == SESSION_HEADER


def test_no_session_header_means_no_session_and_no_session_limit() -> None:
    assert plan(run(SESS_KEY)).session is None


def test_first_call_of_a_session_creates_it_from_the_key_defaults() -> None:
    d = plan(run(SESS_KEY, SESS)).session
    assert d is not None and d.action is SessionAction.CREATE
    assert d.new_state is not None and d.new_state.max_steps == 2


def test_session_at_its_step_limit_is_refused() -> None:
    r = refusal(run(SESS_KEY, SESS, session=live(step_count=2)))
    assert r.code is ErrorCode.SESSION_LIMIT_REACHED and r.details["limit"] == "steps"


def test_session_past_its_time_limit_is_refused() -> None:
    r = refusal(
        run(SESS_KEY, SESS, session=live(started_at=NOW - 61_000, last_activity_at=NOW - 1))
    )
    assert r.details["limit"] == "time"


def test_session_budget_refusal_is_limit_budget_via_check_admission() -> None:
    sb = bud("sess", "session", scope_kind=ScopeKind.SESSION, cap_nano=500)
    planned = plan(run(SESS_KEY, SESS, budgets=[sb], chain_scope_ids=set(SCOPES), session=live()))
    only = planned.budgets[0].budget
    r = check_admission(only, PeriodState(450, 0), 100, NOW, "EUR").refusal
    assert r is not None
    assert r.code is ErrorCode.SESSION_LIMIT_REACHED and r.details["limit"] == "budget"


def test_a_closed_after_limit_session_keeps_refusing_for_the_same_id() -> None:
    closed = live(closed_at=NOW - 500)
    for _ in range(3):
        r = refusal(run(SESS_KEY, SESS, session=closed))
        assert r.code is ErrorCode.SESSION_LIMIT_REACHED


def test_idle_expiry_restarts_and_new_sessions_per_hour_is_enforced() -> None:
    idle = live(last_activity_at=NOW - 31_000)
    d = plan(run(SESS_KEY, SESS, session=idle)).session
    assert d is not None and d.action is SessionAction.RESTART
    capped = key(session_defaults=replace(DEFAULTS, max_new_sessions_per_hour=2))
    r = refusal(run(capped, SESS, session=idle, new_sessions_last_hour=2))
    assert r.details["limit"] == "new_sessions"


def test_two_keys_on_one_scope_never_share_a_session() -> None:
    # Key A's session is full; key B (same scope, same session id) has its own, empty one.
    full = live(step_count=2)
    key_b = replace(SESS_KEY, id="key2")
    assert isinstance(run(SESS_KEY, SESS, session=full), Refusal)
    d = plan(run(key_b, SESS, session=None)).session
    assert d is not None and d.action is SessionAction.CREATE


def test_check_order_is_labels_then_session_header_then_model_then_kill_switch() -> None:
    bad_everything = dict(
        k=key(allowed_models=("none",)), labels=Labels(session="ok", user="bad user")
    )
    assert refusal(run(bad_everything["k"], bad_everything["labels"])).param == USER_HEADER  # type: ignore[arg-type]
    switch = [KillSwitch(KillTarget.GLOBAL)]
    r = refusal(run(key(allowed_models=("none",)), kill_switches=switch))
    assert r.code is ErrorCode.MODEL_NOT_ALLOWED
    assert (
        refusal(run(SESS_KEY, SESS, kill_switches=switch, session=live(step_count=2))).code
        is ErrorCode.KILL_SWITCH
    )
