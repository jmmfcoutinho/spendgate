"""Plain rows the ledger reads return (and `HeldItem`, the unit of a hold).

Split from `port.py` to keep the port readable; `port` re-exports every name, so callers keep
importing them from there.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from spendgate.core.budgets import ScopeKind
from spendgate.core.outcomes import Outcome, ReservationState


@dataclass(frozen=True, slots=True)
class HeldItem:
    budget_id: str
    period_start: int
    held_nano: int


@dataclass(frozen=True, slots=True)
class BudgetTotals:
    """One budget's period row (zeros when nothing was spent) with its effective cap."""

    budget_id: str
    period_start: int
    effective_cap_nano: int
    spent_nano: int
    held_nano: int
    overrun_nano: int
    excess_nano: int
    calls: int
    review_required_at: int | None


@dataclass(frozen=True, slots=True)
class ScopeRow:
    id: str
    kind: ScopeKind
    parent_id: str | None
    name: str
    path: str
    step_count: int = 0
    max_steps: int | None = None
    started_at: int | None = None
    last_activity_at: int | None = None
    closed_at: int | None = None
    closed_reason: str | None = None
    created_at: int = 0
    display_name: str | None = None
    max_seconds: int | None = None  # a session's wall-clock limit (from started_at)


@dataclass(frozen=True, slots=True)
class ReservationRow:
    id: str
    state: ReservationState
    owner_instance: str
    lease_expires_at: int
    key_id: str
    scope_id: str
    session_id: str | None
    outcome: Outcome | None
    charged_nano: int | None
    items: tuple[HeldItem, ...]
    item_overruns: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class CallRow:
    request_id: str
    attempt: int
    provider: str
    deployment: str
    outcome: Outcome
    charged_nano: int
    usage_cost_nano: int | None
    hold_nano: int
    overrun_nano: int
    settled_by: str
    review_required_at: int | None
    finished_at: int
    http_status: int | None = None  # the provider's status for this attempt; None: no answer
