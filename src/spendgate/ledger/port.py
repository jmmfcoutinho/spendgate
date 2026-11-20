"""The ledger contract every backend implements.

Plain data in, plain data out; money is integer nano-units, time is epoch milliseconds passed
in as `now`. The ledger does storage and transactions; every decision (admission, charge,
invariant) comes from `spendgate.core`.
"""

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from spendgate.core.budgets import Budget, Period, ScopeKind, SoftWarning
from spendgate.core.errors import Refusal
from spendgate.core.outcomes import Outcome, ReservationState
from spendgate.core.policy import KeyPolicy, KillSwitch, Plan
from spendgate.ledger.key_types import (
    AdminScope,
    KeyKind,
    KeyRecord,
    KeySpec,
    NewKey,
    StoredKey,
)
from spendgate.ledger.rows import BudgetTotals, CallRow, HeldItem, ReservationRow, ScopeRow
from spendgate.ledger.usage import RetentionResult, UsageQuery, UsageResult

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
class Held:
    request_id: str
    lease_expires_at: int
    items: tuple[HeldItem, ...]
    session_scope_id: str | None
    warnings: tuple[SoftWarning, ...] = ()


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
    error_code: str | None = None
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


@dataclass(frozen=True, slots=True)
class SweepResult:
    expired: tuple[str, ...]  # reservations charged in full
    normalised: tuple[str, ...]  # budgets whose excess was recorded after a grant expired


# --- reads -----------------------------------------------------------------------------------


class LedgerPort(Protocol):
    """Transactions on the money state. Names follow the admin API operation ids."""

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

    # Admin side: the smallest set that creates and changes the state the scenarios run on.
    async def create_scope(  # noqa: PLR0913 - mirrors the admin ScopeCreate body
        self,
        kind: ScopeKind,
        name: str,
        parent_id: str | None,
        *,
        now: int,
        actor: str = "system",
        display_name: str | None = None,
    ) -> ScopeRow: ...

    async def create_budget(  # noqa: PLR0913 - mirrors the admin BudgetCreate body
        self,
        scope_id: str,
        period: Period,
        cap_nano: int,
        *,
        now: int,
        soft_pct: int = 80,
        model_filter: str | None = None,
        strict_overrun: bool = True,
        note: str | None = None,
        actor: str = "system",
        budget_id: str | None = None,
    ) -> Budget: ...

    async def update_budget(  # noqa: PLR0913 - mirrors the admin BudgetUpdate body
        self,
        budget_id: str,
        *,
        actor: str,
        now: int,
        cap_nano: int | None = None,
        soft_pct: int | None = None,
        strict_overrun: bool | None = None,
        note: str | None = None,
    ) -> Budget: ...

    async def create_grant(  # noqa: PLR0913 - mirrors the admin GrantCreate body
        self,
        budget_id: str,
        *,
        amount_nano: int,
        valid_from: int,
        valid_until: int,
        actor: str,
        reason: str,
        now: int,
    ) -> str: ...

    async def ack_overrun(self, budget_id: str, *, actor: str, reason: str, now: int) -> None: ...

    async def engage_kill_switch(
        self, switch: KillSwitch, *, actor: str, reason: str, now: int
    ) -> str: ...

    async def lift_kill_switch(self, switch_id: str, *, actor: str, now: int) -> None: ...

    async def delete_budget(self, budget_id: str, *, actor: str, now: int) -> None: ...

    async def disable_scope(self, scope_id: str, *, actor: str, now: int) -> None: ...

    async def add_key_record(self, key_id: str, scope_id: str, *, now: int) -> None:
        """Thin primitive that satisfies `reservations.key_id`; key flows handle real keys."""
        ...

    # Keys. `pepper` is passed in by the caller and never stored.
    async def create_key(self, spec: KeySpec, *, pepper: bytes, actor: str, now: int) -> NewKey: ...

    async def create_admin_key(  # noqa: PLR0913 - an admin key's whole definition
        self,
        scope_id: str,
        name: str,
        admin_scopes: frozenset[AdminScope],
        *,
        pepper: bytes,
        actor: str,
        now: int,
        expires_at: int | None = None,
    ) -> NewKey: ...

    async def list_keys(
        self, scope_id: str | None = None, *, kind: KeyKind = KeyKind.API
    ) -> tuple[KeyRecord, ...]: ...

    async def get_key(self, key_id: str) -> KeyRecord | None: ...

    async def find_key(self, prefix: str) -> StoredKey | None:
        """Lookup by the public prefix, with the stored hash; used by `keys.KeyService`."""
        ...

    async def revoke_key(self, key_id: str, *, actor: str, now: int) -> KeyRecord: ...

    async def rotate_key(
        self, key_id: str, *, grace_ms: int, pepper: bytes, actor: str, now: int
    ) -> NewKey: ...

    async def set_key_policy(
        self, policy: KeyPolicy, *, name: str | None = None, actor: str, now: int
    ) -> KeyRecord: ...

    async def get_scope(self, scope_id: str) -> ScopeRow | None: ...

    async def active_kill_switches(self) -> tuple[KillSwitch, ...]:
        """Every switch in force now; the pipeline re-checks open streams against them."""
        ...

    async def find_scope(self, path: str) -> ScopeRow | None:
        """A scope by its unique path (`org:acme/user:u1`), or None. Ids never match."""
        ...

    async def get_reservation(self, request_id: str) -> ReservationRow | None: ...

    async def get_calls(self, request_id: str) -> tuple[CallRow, ...]: ...

    # Usage and retention. `retention_days` makes `usage` refuse a `since` older than
    # the configured window even before the first deletion run.
    async def usage(
        self, query: UsageQuery, *, now: int, retention_days: int | None = None
    ) -> UsageResult: ...

    async def delete_calls_older_than(
        self, days: int, now: int, batch: int = 1_000
    ) -> RetentionResult:
        """Whole UTC days of calls (plus old terminal reservations and closed sessions)."""
        ...
