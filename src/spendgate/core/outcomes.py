"""Call outcomes, the `not_processed` predicate and the settlement table.

This is the only place that decides what an outcome costs (ledger-state-machine.md).
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from spendgate.core.money import Money


class Outcome(StrEnum):
    """The one outcome enum, shared with the DDL CHECKs and the OpenAPI spec."""

    OK = "ok"
    OK_NO_USAGE = "ok_no_usage"
    CLIENT_ERROR = "client_error"
    THROTTLED = "throttled"
    TIMEOUT = "timeout"
    UPSTREAM_5XX = "upstream_5xx"
    UNREACHABLE = "unreachable"
    CANCELLED = "cancelled"
    STREAM_BROKEN = "stream_broken"
    INTERNAL_ERROR = "internal_error"
    EXPIRED = "expired"


class ReservationState(StrEnum):
    """Not outcomes: where a reservation is in its life."""

    PENDING = "pending"
    SETTLED = "settled"
    RELEASED = "released"
    EXPIRED = "expired"


class Basis(StrEnum):
    USAGE_OR_ZERO = "usage_or_zero"  # reported usage, else nothing was consumed
    USAGE_OR_HOLD = "usage_or_hold"  # reported usage, else the request may have run: full hold
    FULL_HOLD = "full_hold"  # the sweeper charges the whole hold


@dataclass(frozen=True, slots=True)
class SettlementRule:
    rule_id: str
    basis: Basis
    summary: str


def _rule(rule_id: str, basis: Basis, summary: str) -> SettlementRule:
    return SettlementRule(rule_id, basis, summary)


# One row per outcome, mirroring the settlement rules in ledger-state-machine.md.
SETTLEMENT: Mapping[Outcome, SettlementRule] = MappingProxyType(
    {
        Outcome.OK: _rule("S-01", Basis.USAGE_OR_HOLD, "actual usage; full hold if absent"),
        Outcome.OK_NO_USAGE: _rule("S-02", Basis.USAGE_OR_HOLD, "success without usage: full hold"),
        Outcome.CLIENT_ERROR: _rule("S-03", Basis.USAGE_OR_ZERO, "provider 4xx: rejected, free"),
        Outcome.THROTTLED: _rule("S-04", Basis.USAGE_OR_ZERO, "free_statuses answer: free"),
        Outcome.TIMEOUT: _rule("S-05", Basis.USAGE_OR_HOLD, "sent, no answer: full hold"),
        Outcome.UPSTREAM_5XX: _rule("S-06", Basis.USAGE_OR_HOLD, "408/5xx after send: full hold"),
        Outcome.UNREACHABLE: _rule("S-07", Basis.USAGE_OR_ZERO, "never sent: free"),
        Outcome.CANCELLED: _rule("S-08", Basis.USAGE_OR_HOLD, "cancel after send: full hold"),
        Outcome.STREAM_BROKEN: _rule("S-09", Basis.USAGE_OR_HOLD, "stream cut: full hold"),
        Outcome.INTERNAL_ERROR: _rule("S-10", Basis.USAGE_OR_HOLD, "after contact: full hold"),
        Outcome.EXPIRED: _rule("S-11", Basis.FULL_HOLD, "sweeper: full hold, never refunded"),
    }
)


@dataclass(frozen=True, slots=True)
class Attempt:
    """What is known about one upstream attempt: was it sent, and what status came back."""

    sent: bool
    status: int | None = None

    def __post_init__(self) -> None:
        if not self.sent and self.status is not None:
            raise ValueError("an attempt that was never sent cannot have a status")


def not_processed(attempt: Attempt, free_statuses: Collection[int]) -> bool:
    """True when the provider cannot have done (or billed) the work.

    Never sent, or answered with one of that provider's `free_statuses`. Failover and
    `release` are allowed only when this is true; such an attempt costs 0.
    """
    if not attempt.sent:
        return True
    return attempt.status is not None and attempt.status in free_statuses


def outcome_for_status(status: int, free_statuses: Collection[int]) -> Outcome:
    """Outcome of a provider error answer. Anything odd is charged (fail closed)."""
    if status < 400:  # noqa: PLR2004 - first HTTP error status
        raise ValueError("outcome_for_status takes error statuses (>= 400); 2xx is ok/ok_no_usage")
    if status in free_statuses:
        return Outcome.THROTTLED
    if status == 408 or status >= 500:  # noqa: PLR2004
        return Outcome.UPSTREAM_5XX
    return Outcome.CLIENT_ERROR


def charge(
    outcome: Outcome, actual: Money | None, hold: Money, *, not_processed: bool = False
) -> Money:
    """What the attempt costs. `actual` is the usage cost, None if no usable usage was seen.

    Reported usage is always booked and never clamped to the hold (M-03). `not_processed`
    makes a no-usage attempt free even for outcomes that are normally charged, e.g. an
    `internal_error` before upstream contact.
    """
    rule = SETTLEMENT[outcome]
    if rule.basis is Basis.FULL_HOLD:
        return hold
    if actual is not None:
        hold.require_same_currency(actual)
        return actual
    if not_processed or rule.basis is Basis.USAGE_OR_ZERO:
        return Money.zero(hold.currency)
    return hold


# --- provider rule config (M-10): the only thing a catalog may tune is free_statuses ---------

ALLOWED_FREE_STATUSES = frozenset({429, 529})
DEFAULT_FREE_STATUSES = frozenset({429})
ANTHROPIC_FREE_STATUSES = frozenset({429, 529})


class ProviderRuleError(ValueError):
    """Catalog provider rules that could fail open."""


def validate_free_statuses(statuses: Iterable[int]) -> frozenset[int]:
    """Accept only a subset of [429, 529]."""
    values = list(statuses)
    if any(isinstance(s, bool) or not isinstance(s, int) for s in values):
        raise ProviderRuleError("free_statuses must be integers")
    result = frozenset(values)
    if not result <= ALLOWED_FREE_STATUSES:
        bad = sorted(result - ALLOWED_FREE_STATUSES)
        raise ProviderRuleError(f"free_statuses may only contain 429 and 529, got {bad}")
    return result


def provider_free_statuses(
    provider: str, rules: Mapping[str, object] | None = None
) -> frozenset[int]:
    """Effective `free_statuses` for a provider slug from its catalog `providers` block."""
    rules = rules or {}
    unknown = set(rules) - {"free_statuses"}
    if unknown:
        raise ProviderRuleError(
            f"unknown provider rule(s) {sorted(unknown)}; nothing may fail open"
        )
    if "free_statuses" not in rules:
        return ANTHROPIC_FREE_STATUSES if provider == "anthropic" else DEFAULT_FREE_STATUSES
    value = rules["free_statuses"]
    if not isinstance(value, list | tuple | set | frozenset):
        raise ProviderRuleError("free_statuses must be a list")
    return validate_free_statuses(value)
