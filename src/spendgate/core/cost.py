"""Compute the cost of a call from usage and a price row.

One table (`_RULES`) maps every usage field to its price meter, its unit and its long-context side.
Actual cost and worst case both go through it, so a new meter is a new row, not new code.
Cost is exact in the entry's currency with a single ceiling to nano-units at the end.
"""

from dataclasses import dataclass, fields
from decimal import ROUND_CEILING, Context, Decimal, DivisionByZero, InvalidOperation, Overflow
from typing import Literal

from spendgate.core.catalog import PriceEntry
from spendgate.core.money import Money

type Side = Literal["in", "out"] | None

_MTOK = 1_000_000
_SECONDS_PER_MINUTE = 60
# Ceiling inside the context: an intermediate that runs out of digits can only round up.
_CTX = Context(prec=80, rounding=ROUND_CEILING, traps=[InvalidOperation, DivisionByZero, Overflow])
_ZERO = Decimal(0)


class UnpricedUsage(ValueError):
    """Usage on a meter the entry has no price for; the caller must charge the full hold."""


@dataclass(frozen=True, slots=True)
class Usage:
    """Provider-reported usage, normalised by the adapter.

    Text counters include their cached and cache-write subsets and `output_tokens` includes
    reasoning; the audio counters are separate from the text ones, so no token is billed twice.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    audio_input_tokens: int = 0
    cached_audio_input_tokens: int = 0
    audio_output_tokens: int = 0
    audio_seconds: Decimal = _ZERO
    characters: int = 0
    images: int = 0

    def __post_init__(self) -> None:
        _require_counts(self)


@dataclass(frozen=True, slots=True)
class Estimate:
    """Upper bounds for one request: what it may consume before it is sent.

    `max_output_tokens` bounds `output_tokens + audio_output_tokens` of the reply.
    """

    input_tokens: int = 0
    max_output_tokens: int = 0
    audio_input_tokens: int = 0
    audio_seconds: Decimal = _ZERO
    characters: int = 0
    images: int = 0

    def __post_init__(self) -> None:
        _require_counts(self)


def _require_counts(obj: Usage | Estimate) -> None:
    for f in fields(obj):
        value = getattr(obj, f.name)
        want = Decimal if f.name == "audio_seconds" else int
        if isinstance(value, bool) or not isinstance(value, want):
            raise TypeError(f"{f.name} must be a {want.__name__}, not {type(value).__name__}")
        if value < 0:
            raise ValueError(f"{f.name} must not be negative")


@dataclass(frozen=True, slots=True)
class _Rule:
    field: str  # Usage attribute
    meter: str  # Meters attribute
    per: int  # units per price
    side: Side  # which long-context multiplier applies
    fallback: str | None = None  # meter used when `meter` is absent: conservative, never free


_RULES = (
    _Rule("input_tokens", "input_per_mtok", _MTOK, "in"),
    _Rule("cached_input_tokens", "cached_input_per_mtok", _MTOK, "in", "input_per_mtok"),
    _Rule("cache_write_tokens", "cache_write_per_mtok", _MTOK, "in", "input_per_mtok"),
    _Rule("output_tokens", "output_per_mtok", _MTOK, "out"),
    _Rule("reasoning_tokens", "reasoning_per_mtok", _MTOK, "out", "output_per_mtok"),
    _Rule("audio_input_tokens", "audio_input_per_mtok", _MTOK, "in"),
    _Rule(
        "cached_audio_input_tokens",
        "cached_audio_input_per_mtok",
        _MTOK,
        "in",
        "audio_input_per_mtok",
    ),
    _Rule("audio_output_tokens", "audio_output_per_mtok", _MTOK, "out"),
    _Rule("audio_seconds", "audio_input_per_minute", _SECONDS_PER_MINUTE, None),
    _Rule("characters", "per_1k_characters", 1000, None),
    _Rule("images", "per_image", 1, None),
)
_BY_FIELD = {rule.field: rule for rule in _RULES}
# Worst case bills each group's units at the dearest meter present in the entry.
_DEAREST_INPUT = ("input_per_mtok", "cached_input_per_mtok", "cache_write_per_mtok")
_DEAREST_OUTPUT = ("output_per_mtok", "reasoning_per_mtok", "audio_output_per_mtok")
_DEAREST_AUDIO_TOKENS = ("audio_input_per_mtok", "cached_audio_input_per_mtok")


@dataclass(frozen=True, slots=True)
class _Line:
    units: Decimal
    price: Decimal
    per: int
    side: Side

    @property
    def base(self) -> Decimal:
        return _CTX.divide(_CTX.multiply(self.units, self.price), self.per)

    def scaled(self, factor: Decimal) -> Decimal:
        return _CTX.multiply(self.base, factor)


def _price(entry: PriceEntry, meter: str) -> Decimal | None:
    price: Decimal | None = getattr(entry.meters, meter)
    return price


def _dearest(entry: PriceEntry, meters: tuple[str, ...]) -> Decimal | None:
    prices = [p for m in meters if (p := _price(entry, m)) is not None]
    return max(prices) if prices else None


def _round_seconds(seconds: Decimal, granularity: int) -> Decimal:
    # Whole granularity units, rounded up, at least one unit for any non-zero duration.
    if seconds == 0:
        return _ZERO
    units = _CTX.divide(seconds, granularity).to_integral_value(rounding=ROUND_CEILING)
    return _CTX.multiply(max(units, Decimal(1)), granularity)


def _billable(entry: PriceEntry, usage: Usage) -> dict[str, Decimal]:
    # Partition the text counters so no token is billed twice; over-reported subsets are billed
    # in full (never under-state) and simply leave nothing for the parent meter.
    plain_input = max(usage.input_tokens - usage.cached_input_tokens - usage.cache_write_tokens, 0)
    plain_output = max(usage.output_tokens - usage.reasoning_tokens, 0)
    seconds = _round_seconds(usage.audio_seconds, entry.meters.audio_granularity_seconds)
    units = {f.name: Decimal(getattr(usage, f.name)) for f in fields(usage)}
    return units | {
        "input_tokens": Decimal(plain_input),
        "output_tokens": Decimal(plain_output),
        "audio_seconds": seconds,
    }


def _priced_lines(entry: PriceEntry, usage: Usage) -> list[_Line]:
    lines = []
    for field, units in _billable(entry, usage).items():
        if units == 0:
            continue
        rule = _BY_FIELD[field]
        price = _price(entry, rule.meter)
        if price is None and rule.fallback is not None:
            price = _price(entry, rule.fallback)
        if price is None:
            raise UnpricedUsage(f"{entry.id} has no price for {rule.meter} (usage: {field})")
        lines.append(_Line(units, price, rule.per, rule.side))
    return lines


def _total(entry: PriceEntry, lines: list[_Line], *, long_tier: bool, worst: bool) -> Money:
    lc = entry.long_context
    total = _ZERO
    for line in lines:
        factor = Decimal(1)
        if long_tier and lc is not None and line.side is not None:
            factor = lc.input_multiplier if line.side == "in" else lc.output_multiplier
            if worst:  # the real request may stay in the short tier: never go below it
                factor = max(factor, Decimal(1))
        total = _CTX.add(total, line.scaled(factor))
    return Money.from_amount(_CTX.multiply(total, entry.price_multiplier), entry.currency)


def actual_cost(entry: PriceEntry, usage: Usage) -> Money:
    """Cost of reported usage in the entry's currency (convert once with `Catalog.in_ledger`)."""
    lc = entry.long_context
    long_tier = lc is not None and usage.input_tokens > lc.threshold_input_tokens
    return _total(entry, _priced_lines(entry, usage), long_tier=long_tier, worst=False)


def _audio_line(entry: PriceEntry, est: Estimate) -> list[_Line]:
    # Minute and token meters are alternatives for one clip; reserve the dearer one.
    candidates = []
    if est.audio_seconds > 0 and (minute := _price(entry, "audio_input_per_minute")) is not None:
        seconds = _round_seconds(est.audio_seconds, entry.meters.audio_granularity_seconds)
        candidates.append(_Line(seconds, minute, _SECONDS_PER_MINUTE, None))
    tokens = _dearest(entry, _DEAREST_AUDIO_TOKENS)
    if est.audio_input_tokens > 0 and tokens is not None:
        candidates.append(_Line(Decimal(est.audio_input_tokens), tokens, _MTOK, "in"))
    if (est.audio_seconds > 0 or est.audio_input_tokens > 0) and not candidates:
        raise UnpricedUsage(f"{entry.id} has no audio meter for this request")
    return [max(candidates, key=lambda c: c.base)] if candidates else []


def _worst_lines(entry: PriceEntry, est: Estimate) -> list[_Line]:
    lines = _audio_line(entry, est)
    text: tuple[tuple[int, tuple[str, ...], Side], ...] = (
        (est.input_tokens, _DEAREST_INPUT, "in"),
        (est.max_output_tokens, _DEAREST_OUTPUT, "out"),
    )
    for count, group, side in text:
        if count == 0:
            continue
        price = _dearest(entry, group)
        if price is None:
            raise UnpricedUsage(f"{entry.id} has no {side}put token meter")
        lines.append(_Line(Decimal(count), price, _MTOK, side))
    for field in ("characters", "images"):
        count = getattr(est, field)
        rule = _BY_FIELD[field]
        if count == 0:
            continue
        price = _price(entry, rule.meter)
        if price is None:
            raise UnpricedUsage(f"{entry.id} has no price for {rule.meter}")
        lines.append(_Line(Decimal(count), price, rule.per, rule.side))
    return lines


def worst_case(entry: PriceEntry, estimate: Estimate) -> Money:
    """Most the request can cost: dearest meters, no cache discount, dearer long-context tier."""
    lc = entry.long_context
    long_tier = lc is not None and estimate.input_tokens > lc.threshold_input_tokens
    return _total(entry, _worst_lines(entry, estimate), long_tier=long_tier, worst=True)
