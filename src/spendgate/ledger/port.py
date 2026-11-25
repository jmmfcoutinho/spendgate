"""The ledger contract every backend implements.

Plain data in, plain data out; money is integer nano-units, time is epoch milliseconds passed
in as `now`. The ledger does storage and transactions; every decision (admission, charge,
invariant) comes from `spendgate.core`.
"""

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from spendgate.core.budgets import BudgetAlert, SoftWarning
from spendgate.core.errors import Refusal
from spendgate.core.failures import CallErrorCode
from spendgate.core.outcomes import Outcome, ReservationState
from spendgate.core.policy import KeyPolicy, KillSwitch, Plan
from spendgate.ledger.rows import BudgetTotals, CallRow, HeldItem, ReservationRow, ScopeRow

# Row types live in `rows`; they are re-exported here because every caller imports them from `port`.
__all__ = ["BudgetTotals", "CallRow", "HeldItem", "ReservationRow", "ScopeRow"]

NANO_PER_UNIT = 10**9


class LedgerError(Exception):
    """Base of every error the ledger raises on purpose."""


class ConflictError(LedgerError):
    """A hold on a request id that already exists, in any state (never an idempotent replay)."""


class NotFoundError(LedgerError):
    """The scope, budget, key, switch or reservation does not exist."""


class CurrencyMismatch(LedgerError):
    """The ledger was created in another currency than the one it is opened with."""


class LedgerUnavailable(LedgerError):
    """The store cannot be used; the caller fails closed (`ledger_unavailable`)."""


# --- hold ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """One deployment the request may be sent to; the sweeper needs one to write its calls row."""

    provider: str
    deployment: str
    price_id: str
    native_currency: str = "USD"
    fx_rate_nano: int = NANO_PER_UNIT

    def as_json(self) -> dict[str, str | int]:
        return {
            "provider": self.provider,
            "deployment": self.deployment,
            "price_id": self.price_id,
            "native_currency": self.native_currency,
            "fx_rate_nano": self.fx_rate_nano,
        }


@dataclass(frozen=True, slots=True)
class HoldMeta:
    """Stored as JSON on the reservation so the sweeper can write a complete `calls` row."""

    endpoint: str
    model: str
    catalog_sha: str
    candidates: tuple[Candidate, ...]
    user_label: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "endpoint": self.endpoint,
                "model": self.model,
                "catalog_sha": self.catalog_sha,
                "user_label": self.user_label,
                "labels": dict(self.labels),
                "candidates": [c.as_json() for c in self.candidates],
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> "HoldMeta":
        raw = json.loads(text)
        return cls(
            raw["endpoint"],
            raw["model"],
            raw["catalog_sha"],
            tuple(Candidate(**c) for c in raw["candidates"]),
            raw["user_label"],
            raw["labels"],
        )


@dataclass(frozen=True, slots=True)
class HoldRequest:
    """Everything the admission transaction needs.

    `plan` is optional: the transaction re-derives the budget chain from the store under its
    locks, and unions in any budget the plan names (a stale plan can only add, never remove).
    """

    request_id: str
    instance: str  # owner of the lease
    now: int
    lease_ms: int
    worst_nano: int  # max over candidate deployments
    key: KeyPolicy
    model: str
    providers: tuple[str, ...]  # every candidate's provider, for provider kill switches
    meta: HoldMeta
    session: str | None = None  # the X-Spendgate-Session value
    plan: Plan | None = None


@dataclass(frozen=True, slots=True)
class BudgetRoom:
    """Room in one budget as the transition that returned it left it: cap minus spent minus held.

    Read under that transition's lock, so headers need no read of their own. Negative after an
    overrun; whoever shows it clamps at zero.
    """

    budget_id: str
    scope_id: str
    room_nano: int


@dataclass(frozen=True, slots=True)
class Held:
    request_id: str
    lease_expires_at: int
    items: tuple[HeldItem, ...]
    session_scope_id: str | None
    warnings: tuple[SoftWarning, ...] = ()
    alerts: tuple[BudgetAlert, ...] = ()  # soft-threshold crossings this hold made, once per period
    rooms: tuple[BudgetRoom, ...] = ()  # each held budget after this hold


@dataclass(frozen=True, slots=True)
class Refused:
    """A refusal as data; `refusal.details` carries scope and remaining."""

    refusal: Refusal


# --- settle / release ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    tokens_cache_write: int = 0
    tokens_reasoning: int = 0
    audio_tokens_in: int = 0
    audio_tokens_out: int = 0
    audio_ms: int = 0
    characters: int = 0
    images: int = 0


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One provider attempt, becomes one `calls` row. The common fields come from `HoldMeta`."""

    attempt: int  # 1..n
    provider: str
    deployment: str
    price_id: str
    outcome: Outcome
    charged_nano: int  # money booked for this attempt; the attempts of one settle sum to its charge
    started_at: int
    finished_at: int
    not_processed: bool = False  # core.outcomes.not_processed, decided by the caller
    native_currency: str = "USD"
    fx_rate_nano: int = NANO_PER_UNIT
    native_cost_nano: int = 0
    usage_cost_nano: int | None = None  # None when no usage was seen
    usage: Usage = field(default_factory=Usage)
    http_status: int | None = None
    error_code: CallErrorCode | None = None  # a documented code (data-model.md), never free text
    streamed: bool = False
    review_required: bool = False  # the C-22 sanity flag; the usage is still booked in full
    latency_ms: int | None = None
    ttfb_ms: int | None = None


@dataclass(frozen=True, slots=True)
class Settlement:
    """What a settle or release did; `applied` False = already terminal before this call."""

    applied: bool
    state: ReservationState  # the reservation's state after the call
    charged_nano: int = 0  # booked to every budget now (late settle: the extra overrun only)
    overrun_nano: int = 0
    late: bool = False
    review_flagged: tuple[str, ...] = ()  # budgets that a breach overrun just flagged
    alerts: tuple[BudgetAlert, ...] = ()  # cap crossings and blocks this settlement made
    rooms: tuple[BudgetRoom, ...] = ()  # each enabled budget it booked; empty when not applied


@dataclass(frozen=True, slots=True)
class PoolStats:
    """A connection pool right now (Postgres only), for the gauges at `/metrics`."""

    size: int  # connections the pool holds: idle, lent out or still being opened
    in_use: int  # lent out to a transaction
    max_size: int


@dataclass(frozen=True, slots=True)
class SweepResult:
    expired: tuple[str, ...]  # reservations charged in full
    normalised: tuple[str, ...]  # budgets whose excess was recorded after a grant expired


# --- the money port ---------------------------------------------------------------------------


class MoneyLedger(Protocol):
    """The transactions and reads a call needs: everything the pipeline and the sweeper use.

    Narrow on purpose: a test double for the money path implements these eight methods, and
    nothing on the request path can reach an admin write. `admin_port.AdminLedger` adds the rest.
    """

    async def hold(self, request: HoldRequest) -> Held | Refused: ...

    async def settle(
        self, request_id: str, charge_nano: int, attempts: tuple[AttemptRecord, ...], now: int
    ) -> Settlement: ...

    async def release(
        self, request_id: str, attempts: tuple[AttemptRecord, ...], now: int
    ) -> Settlement: ...

    async def renew_all(
        self, instance: str, ids: Collection[str], now: int, lease_ms: int
    ) -> tuple[str, ...]:
        """Extend the lease of exactly `ids` (those still pending and owned by `instance`).

        Never every hold of the instance: a hold its owner failed to close must expire so the
        sweeper charges it. Returns the ids renewed.
        """
        ...

    async def sweep_expired(self, now: int, batch: int = 100) -> SweepResult: ...

    async def totals(self, scope_id: str, now: int) -> tuple[BudgetTotals, ...]: ...

    async def get_scope(self, scope_id: str) -> ScopeRow | None: ...

    async def active_kill_switches(self) -> tuple[KillSwitch, ...]:
        """Every switch in force now; the pipeline re-checks open streams against them."""
        ...
