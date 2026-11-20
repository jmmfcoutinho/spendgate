"""Session gating (rule M-11)."""

import pytest

from spendgate.core.errors import ErrorCode, LimitName, Refusal
from spendgate.core.sessions import (
    SessionAction,
    SessionDefaults,
    SessionState,
    gate_session,
    session_path,
)

S = 1_000
T0 = 1_000_000
DEFAULTS = SessionDefaults(
    budget_nano=5_000, max_steps=5, max_seconds=3_600, idle_seconds=600, max_new_sessions_per_hour=3
)


def state(**kw: object) -> SessionState:
    base = SessionState(T0, T0, 0, 5, 3_600, 600)
    return _with(base, kw)


def _with(base: SessionState, kw: dict[str, object]) -> SessionState:
    fields = {f: getattr(base, f) for f in base.__slots__}
    fields.update(kw)
    return SessionState(**fields)


def limit_of(refusal: Refusal | None) -> str:
    assert refusal is not None
    assert refusal.code is ErrorCode.SESSION_LIMIT_REACHED
    return str(refusal.details["limit"])


def test_first_use_creates_a_session_with_the_key_defaults() -> None:
    d = gate_session(DEFAULTS, None, T0, 0)
    assert d.action is SessionAction.CREATE and d.refusal is None
    assert d.new_state == SessionState(T0, T0, 0, 5, 3_600, 600)


def test_a_live_session_continues() -> None:
    d = gate_session(DEFAULTS, state(step_count=3), T0 + 100 * S, 0)
    assert d.action is SessionAction.CONTINUE and d.new_state is None


def test_m11_idle_expiry_restarts_with_a_fresh_step_counter() -> None:
    now = T0 + 601 * S
    d = gate_session(DEFAULTS, state(step_count=3), now, 0)
    assert d.action is SessionAction.RESTART
    assert d.new_state is not None and d.new_state.step_count == 0
    assert d.new_state.started_at == now


def test_idle_boundary_is_exclusive() -> None:
    assert gate_session(DEFAULTS, state(), T0 + 600 * S, 0).action is SessionAction.CONTINUE


def test_no_idle_limit_never_restarts() -> None:
    defaults = SessionDefaults(max_steps=5)
    old = state(idle_seconds=None, max_seconds=None)
    assert gate_session(defaults, old, T0 + 10**9, 0).action is SessionAction.CONTINUE


def test_session_at_its_step_limit_is_refused_with_limit_steps() -> None:
    d = gate_session(DEFAULTS, state(step_count=5), T0 + S, 0)
    assert d.action is SessionAction.REFUSE and d.refusal is not None
    assert limit_of(d.refusal) == "steps"
    assert d.refusal.http_status == 402


def test_session_past_its_time_limit_is_refused_with_limit_time() -> None:
    at_limit = state(last_activity_at=T0 + 3_599 * S)
    assert gate_session(DEFAULTS, at_limit, T0 + 3_600 * S, 0).action is SessionAction.CONTINUE
    d = gate_session(DEFAULTS, state(last_activity_at=T0 + 3_600 * S), T0 + 3_601 * S, 0)
    assert d.refusal is not None and limit_of(d.refusal) == "time"


def test_l_k_limits_are_checked_before_idle_so_a_limit_session_never_restarts() -> None:
    idle_and_full = state(step_count=5)
    d = gate_session(DEFAULTS, idle_and_full, T0 + 599 * S + 600 * S, 0)
    assert d.action is SessionAction.REFUSE and d.refusal is not None
    assert limit_of(d.refusal) == "steps"


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"closed_at": T0 + 10 * S, "closed_reason": LimitName.STEPS}, "steps"),
        ({"closed_at": T0 + 3_700 * S, "closed_reason": LimitName.TIME}, "time"),
        ({"closed_at": T0 + 10 * S, "closed_reason": LimitName.BUDGET}, "budget"),
        # The stored reason wins over what the row looks like (steps are used up here).
        ({"closed_at": T0 + 10 * S, "closed_reason": LimitName.BUDGET, "step_count": 5}, "budget"),
        # Closed without a reason must never reopen: fail closed.
        ({"closed_at": T0 + 10 * S}, "budget"),
    ],
)
def test_l_k_a_closed_session_keeps_refusing_and_names_why(
    kw: dict[str, object], expected: str
) -> None:
    closed = state(**kw)
    for later in (T0 + S, T0 + 10**7):  # even long after the idle limit: no restart
        d = gate_session(DEFAULTS, closed, later, 0)
        assert d.refusal is not None and limit_of(d.refusal) == expected


def test_closed_reason_needs_closed_at_and_a_session_limit() -> None:
    with pytest.raises(ValueError, match="closed_reason"):
        state(closed_reason=LimitName.STEPS)
    with pytest.raises(ValueError, match="closed_reason"):
        state(closed_at=T0, closed_reason=LimitName.RPM)


def test_m11_max_new_sessions_per_hour_gates_creation_and_restart() -> None:
    created = gate_session(DEFAULTS, None, T0, 2)
    assert created.action is SessionAction.CREATE
    blocked = gate_session(DEFAULTS, None, T0, 3)
    assert blocked.refusal is not None and limit_of(blocked.refusal) == "new_sessions"
    idle = state()
    restart_blocked = gate_session(DEFAULTS, idle, T0 + 700 * S, 3)
    assert restart_blocked.refusal is not None
    assert limit_of(restart_blocked.refusal) == "new_sessions"
    # A live session is not a new session, so the rate limit does not touch it.
    assert gate_session(DEFAULTS, idle, T0 + S, 99).action is SessionAction.CONTINUE


def test_defaults_without_a_rate_limit_allow_any_number_of_new_sessions() -> None:
    assert gate_session(SessionDefaults(), None, T0, 10**6).action is SessionAction.CREATE


def test_defaults_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_steps"):
        SessionDefaults(max_steps=0)


def test_sessions_are_per_key() -> None:
    a = session_path("org:acme/project:app", "key-a", "nightly")
    b = session_path("org:acme/project:app", "key-b", "nightly")
    assert a == "org:acme/project:app/session:key-a:nightly"
    assert a != b
