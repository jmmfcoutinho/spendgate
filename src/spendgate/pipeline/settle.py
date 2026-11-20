"""Step 9: the attempts of one request and the one money transition that closes them.

`Book` is the pipeline's memory of a request after the hold: finished attempts, the attempt in
flight, and what the last attempt is worth. `decide` turns it into exactly one ledger call:
`release` when every attempt was `not_processed` (nothing may have been done), `settle` otherwise.
Charging comes only from `core.outcomes.charge` (M-13); nothing here prices a fact twice.
"""

import logging
from dataclasses import astuple, dataclass, field, replace
from decimal import Decimal

from spendgate.core.catalog import Catalog, PriceEntry
from spendgate.core.cost import UnpricedUsage, Usage, actual_cost
from spendgate.core.money import Money
from spendgate.core.outcomes import Outcome, charge
from spendgate.ledger.port import AttemptRecord
from spendgate.ledger.port import Usage as LedgerUsage

_MS = 1000
INT64_MAX = 2**63 - 1  # the ledger's integer column: nothing larger can be booked as reported
_log = logging.getLogger("spendgate.pipeline")
# C-22: reported usage worth more than this many holds is booked in full but flagged for review.
# The hold already contains the full output ceiling, so an honest call stays far below it.
SANITY_FACTOR = 10


@dataclass(frozen=True, slots=True)
class Pricing:
    """The catalog entry that prices the request and what the hold is worth in ledger money."""

    entry: PriceEntry
    fx_rate_nano: int
    worst: Money  # ledger currency: the hold


@dataclass(frozen=True, slots=True)
class Reading:
    """What one attempt's reported usage is worth, priced once (CR-04, CR-08).

    `cost` None means "charge the full hold": no usage, a meter the entry cannot price, or
    (`absurd`) usage the ledger cannot store. An absurd reading keeps no usage at all.
    """

    usage: Usage | None = None
    native: Money | None = None
    cost: Money | None = None  # ledger currency
    absurd: bool = False


@dataclass(frozen=True, slots=True)
class Flight:
    """An attempt whose request is being sent: a cancel from here on counts as sent (M-13)."""

    number: int
    provider: str
    deployment: str
    started_at: int


@dataclass(slots=True)
class Book:
    """Attempts of one request, in order. Charges are filled in by `decide`, not earlier."""

    pricing: Pricing
    label: str  # first candidate's deployment label: names an attempt-less release in events
    idle_outcome: Outcome = Outcome.INTERNAL_ERROR  # how an attempt-less release is labelled
    attempts: list[AttemptRecord] = field(default_factory=list)
    flight: Flight | None = None
    actual: Money | None = None  # ledger-currency cost of the last attempt's usage, if known
    usage: Usage | None = None

    def note(self, reading: Reading) -> None:
        """Remember what the attempt that just ended is worth (what `decide` charges)."""
        self.usage, self.actual = reading.usage, reading.cost

    @property
    def next_number(self) -> int:
        return len(self.attempts) + 1


@dataclass(frozen=True, slots=True)
class Decision:
    """The single ledger transition for a request."""

    release: bool
    charge: Money
    attempts: tuple[AttemptRecord, ...]
    outcome: Outcome
    deployment: str


@dataclass(frozen=True, slots=True)
class Closed:
    """The result of closing a request: the decision, whether the ledger took it, what is left."""

    decision: Decision
    booked: bool  # False: the ledger could not be reached; the sweeper charges the hold
    remaining: tuple[Money, str] | None = None  # tightest budget room and its scope path


def ledger_usage(usage: Usage) -> LedgerUsage:
    return LedgerUsage(
        tokens_in=usage.input_tokens,
        tokens_out=usage.output_tokens,
        tokens_cached=usage.cached_input_tokens,
        tokens_cache_write=usage.cache_write_tokens,
        tokens_reasoning=usage.reasoning_tokens,
        audio_tokens_in=usage.audio_input_tokens + usage.cached_audio_input_tokens,
        audio_tokens_out=usage.audio_output_tokens,
        audio_ms=int(usage.audio_seconds * _MS),
        characters=usage.characters,
        images=usage.images,
    )


def _fits(usage: Usage) -> bool:
    return all(abs(v) <= INT64_MAX for v in astuple(ledger_usage(usage)))


def read_usage(catalog: Catalog, entry: PriceEntry, usage: Usage | None) -> Reading:
    """Price reported usage once. Anything unreadable or unstorable charges the full hold.

    A meter the entry cannot price keeps the usage for the record but no cost. Usage whose
    counters or price do not fit the ledger's int64 is `absurd` (C-22, M-03): the hold is charged
    and the call is flagged for review; it is never an exception that leaves the hold pending.
    """
    if usage is None:
        return Reading()
    try:
        if not _fits(usage):
            return Reading(absurd=True)
        native = actual_cost(entry, usage)
        cost = catalog.in_ledger(native)
    except UnpricedUsage:
        return Reading(usage)
    except (ArithmeticError, ValueError):  # a Decimal overflow, an impossible conversion
        return Reading(absurd=True)
    if max(native.nano, cost.nano) > INT64_MAX:
        return Reading(absurd=True)
    return Reading(usage, native, cost)


def needs_review(request_id: str, reading: Reading, hold: Money) -> bool:
    """True when the reading was unstorable or costs more than `SANITY_FACTOR` holds."""
    if reading.absurd:
        _log.warning("unstorable provider usage flagged for review request_id=%s", request_id)
        return True
    return flag_review(request_id, reading.cost, hold)


def flag_review(request_id: str, actual: Money | None, hold: Money) -> bool:
    """C-22: True (and a warning) when reported usage costs more than `SANITY_FACTOR` holds.

    Only a flag: the usage is booked as reported, never clamped (M-03). The warning carries the
    request id and the two amounts, nothing from the request or the provider's text.
    """
    if actual is None or actual.nano <= SANITY_FACTOR * hold.nano:
        return False
    _log.warning(
        "absurd provider usage flagged for review request_id=%s actual=%s hold=%s",
        request_id, actual.format(), hold.format(),
    )  # fmt: skip
    return True


def fx_rate_nano(catalog: Catalog, currency: str) -> int:
    """Units of ledger money per unit of `currency`, as integer nano, for the calls row."""
    one = Money.from_amount(Decimal(1), currency)
    return catalog.in_ledger(one).nano


def attempt_record(  # noqa: PLR0913 - one attempt's facts, built in exactly one place
    book: Book, flight: Flight, outcome: Outcome, now: int, *, request_id: str = "",
    status: int | None = None, not_processed: bool = False, error_code: str | None = None,
    reading: Reading | None = None, streamed: bool = False, ttfb_ms: int | None = None,
) -> AttemptRecord:  # fmt: skip
    """The `calls` row of one attempt (buffered, streamed or interrupted)."""
    entry, read = book.pricing.entry, reading or Reading()
    return AttemptRecord(
        flight.number, flight.provider, flight.deployment, entry.price_id, outcome, 0,
        flight.started_at, now, not_processed, entry.currency, book.pricing.fx_rate_nano,
        native_cost_nano=read.native.nano if read.native else 0,
        usage_cost_nano=read.cost.nano if read.cost else None,
        usage=ledger_usage(read.usage) if read.usage is not None else LedgerUsage(),
        http_status=status, error_code=error_code, streamed=streamed,
        review_required=needs_review(request_id, read, book.pricing.worst) if reading else False,
        latency_ms=max(now - flight.started_at, 0), ttfb_ms=ttfb_ms,
    )  # fmt: skip


def decide(book: Book, interrupted: Outcome | None, now: int) -> Decision:
    """Release or settle. `interrupted` is how the in-flight attempt ended without an answer
    (cancel, gateway error): it counts as sent, so it settles at the full hold unless usage was
    seen (it cannot have been)."""
    hold = book.pricing.worst
    attempts = list(book.attempts)
    if book.flight is not None:
        outcome = interrupted or Outcome.INTERNAL_ERROR
        attempts.append(attempt_record(book, book.flight, outcome, now, error_code=outcome.value))
    if not attempts:  # nothing was ever sent: release with no `calls` row at all
        return Decision(True, Money.zero(hold.currency), (), interrupted or book.idle_outcome,
                        book.label)  # fmt: skip
    if all(a.not_processed for a in attempts):
        released = tuple(replace(a, charged_nano=0) for a in attempts)
        last = attempts[-1]
        return Decision(True, Money.zero(hold.currency), released, last.outcome, last.deployment)
    last = attempts[-1]
    actual = book.actual if book.flight is None else None
    amount = charge(last.outcome, actual, hold, not_processed=last.not_processed)
    attempts[-1] = replace(last, charged_nano=amount.nano)
    return Decision(False, amount, tuple(attempts), last.outcome, last.deployment)
