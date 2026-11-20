"""Agent-session gating: limits come only from the key's `session_defaults`."""

from dataclasses import dataclass
from enum import StrEnum

from spendgate.core.errors import ErrorCode, LimitName, Refusal

_MS_PER_S = 1000
_CLOSE_REASONS = frozenset({LimitName.STEPS, LimitName.TIME, LimitName.BUDGET})


@dataclass(frozen=True, slots=True)
class SessionDefaults:
    """Per-key session limits. None = that limit is not enforced."""

    budget_nano: int | None = None
    max_steps: int | None = None
    max_seconds: int | None = None
    idle_seconds: int | None = None
    max_new_sessions_per_hour: int | None = None

    def __post_init__(self) -> None:
        for name in self.__slots__:
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"session default {name} must be positive")


@dataclass(frozen=True, slots=True)
class SessionState:
    """A session row. The limits were copied from the key's defaults when it was created."""

    started_at: int
    last_activity_at: int
    step_count: int = 0
    max_steps: int | None = None
    max_seconds: int | None = None
    idle_seconds: int | None = None
    closed_at: int | None = None
    # Why a limit closed it, stored with `closed_at` (never re-derived from the row, which cannot
    # tell a budget close from a steps close). None with `closed_at` set = closed by idle restart.
    closed_reason: LimitName | None = None

    def __post_init__(self) -> None:
        if self.closed_reason is not None and (
            self.closed_at is None or self.closed_reason not in _CLOSE_REASONS
        ):
            raise ValueError("closed_reason needs closed_at and one of steps, time, budget")

    @classmethod
    def fresh(cls, defaults: SessionDefaults, now: int) -> "SessionState":
        return cls(now, now, 0, defaults.max_steps, defaults.max_seconds, defaults.idle_seconds)


class SessionAction(StrEnum):
    CONTINUE = "continue"
    CREATE = "create"
    RESTART = "restart"  # close and rename the old row, then create `new_state`
    REFUSE = "refuse"


@dataclass(frozen=True, slots=True)
class SessionDecision:
    action: SessionAction
    refusal: Refusal | None = None
    new_state: SessionState | None = None


def session_path(parent_path: str, key_id: str, session_id: str) -> str:
    """Sessions are per key: two keys on one scope never share one."""
    return f"{parent_path}/session:{key_id}:{session_id}"


def _refuse(limit: LimitName) -> SessionDecision:
    refusal = Refusal(
        ErrorCode.SESSION_LIMIT_REACHED,
        f"Session limit reached: {limit.value}.",
        None,
        {"limit": limit.value},
    )
    return SessionDecision(SessionAction.REFUSE, refusal)


def _steps_done(state: SessionState) -> bool:
    return state.max_steps is not None and state.step_count >= state.max_steps


def _time_done(state: SessionState, at: int) -> bool:
    return state.max_seconds is not None and at - state.started_at > state.max_seconds * _MS_PER_S


def _limit_hit(state: SessionState, now: int) -> LimitName | None:
    if state.closed_at is not None:
        # A row closed without a reason is an idle-restarted one, renamed and never looked up;
        # if one does arrive, fail closed as a budget close.
        return state.closed_reason or LimitName.BUDGET
    if _steps_done(state):
        return LimitName.STEPS
    return LimitName.TIME if _time_done(state, now) else None


def _idle(state: SessionState, now: int) -> bool:
    if state.idle_seconds is None:
        return False
    return now - state.last_activity_at > state.idle_seconds * _MS_PER_S


def gate_session(
    defaults: SessionDefaults, state: SessionState | None, now: int, new_last_hour: int
) -> SessionDecision:
    """Decide what the session header means for this call.

    A session that hit a limit stays closed and keeps refusing (limits are checked before
    idleness); only an idle-expired one restarts, and a new or restarted session must fit
    `max_new_sessions_per_hour` (`new_last_hour` counts this key's sessions in the last hour).
    """
    action = SessionAction.CREATE
    if state is not None:
        hit = _limit_hit(state, now)
        if hit is not None:
            return _refuse(hit)
        if not _idle(state, now):
            return SessionDecision(SessionAction.CONTINUE)
        action = SessionAction.RESTART
    cap = defaults.max_new_sessions_per_hour
    if cap is not None and new_last_hour >= cap:
        return _refuse(LimitName.NEW_SESSIONS)
    return SessionDecision(action, None, SessionState.fresh(defaults, now))
