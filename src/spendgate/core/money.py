"""Integer nano-unit money. Every rounding goes up, so a cost is never under-stated.

`Money` is an immutable amount in integer nano-units (1e-9) of one currency. Arithmetic and
comparisons are allowed only within one currency. `float` is rejected at every edge.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Context, Decimal, DivisionByZero, InvalidOperation, Overflow

NANO_PER_UNIT = 10**9

# Ceiling rounding in the context itself: even an intermediate product that runs out of
# digits can only round up. 80 digits is far beyond any real price times any real volume.
_CTX = Context(prec=80, rounding=ROUND_CEILING, traps=[InvalidOperation, DivisionByZero, Overflow])
_CURRENCY = re.compile(r"[A-Z]{3}")
_AMOUNT = re.compile(r"\s*(\d+(?:\.\d+)?)\s+([A-Za-z]{3})\s*")

type Number = Decimal | str | int


class MoneyError(ValueError):
    """Invalid amount, currency or rate."""


class CurrencyMismatch(MoneyError):
    """Two currencies were mixed without an FX conversion."""


class MissingFxRate(MoneyError):
    """No FX rate for a needed currency pair; the caller must refuse, never default (M-09)."""


def _decimal(value: object) -> Decimal:
    # bool is an int subclass; True as an amount is a bug, not a number.
    if isinstance(value, bool | float) or not isinstance(value, Decimal | str | int):
        raise TypeError(f"money maths takes Decimal, str or int, not {type(value).__name__}")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise MoneyError(f"not a number: {value!r}") from exc
    if not result.is_finite():
        raise MoneyError(f"not a finite number: {value!r}")
    return result


def _ceil_nano(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _check_currency(code: str) -> str:
    if not isinstance(code, str) or not _CURRENCY.fullmatch(code):
        raise MoneyError(f"currency must be a 3-letter upper-case code, got {code!r}")
    return code


@dataclass(frozen=True, slots=True)
class Money:
    """An amount of `nano` nano-units of `currency`."""

    nano: int
    currency: str

    def __post_init__(self) -> None:
        if isinstance(self.nano, bool) or not isinstance(self.nano, int):
            raise TypeError("Money.nano must be an int (nano-units)")
        _check_currency(self.currency)

    @classmethod
    def zero(cls, currency: str) -> "Money":
        return cls(0, currency)

    @classmethod
    def from_amount(cls, amount: Number, currency: str) -> "Money":
        """Whole-unit amount (`"12.5"`) to nano-units, rounded up."""
        return cls(_ceil_nano(_CTX.multiply(_decimal(amount), NANO_PER_UNIT)), currency)

    def to_decimal(self) -> Decimal:
        return Decimal(self.nano) / NANO_PER_UNIT

    def format(self) -> str:
        """Decimal string with 9 decimals and the currency, e.g. `12.500000000 EUR`."""
        sign = "-" if self.nano < 0 else ""
        whole, frac = divmod(abs(self.nano), NANO_PER_UNIT)
        return f"{sign}{whole}.{frac:09d} {self.currency}"

    def require_same_currency(self, other: "Money") -> None:
        """Raise `CurrencyMismatch` unless `other` is in the same currency."""
        if not isinstance(other, Money):
            raise TypeError("Money combines only with Money")
        if other.currency != self.currency:
            raise CurrencyMismatch(f"cannot mix {self.currency} and {other.currency}")

    def __add__(self, other: "Money") -> "Money":
        self.require_same_currency(other)
        return Money(self.nano + other.nano, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self.require_same_currency(other)
        return Money(self.nano - other.nano, self.currency)

    def __mul__(self, factor: int) -> "Money":
        if isinstance(factor, bool) or not isinstance(factor, int):
            raise TypeError("Money multiplies by an int only; use from_price for rates")
        return Money(self.nano * factor, self.currency)

    def __lt__(self, other: "Money") -> bool:
        self.require_same_currency(other)
        return self.nano < other.nano

    def __le__(self, other: "Money") -> bool:
        self.require_same_currency(other)
        return self.nano <= other.nano

    def __gt__(self, other: "Money") -> bool:
        self.require_same_currency(other)
        return self.nano > other.nano

    def __ge__(self, other: "Money") -> bool:
        self.require_same_currency(other)
        return self.nano >= other.nano


def parse_amount(text: str) -> Money:
    """Parse `"5 USD"` or `"0.25 EUR"` (the library's `cap=` form). Rounds up past 9 decimals."""
    match = _AMOUNT.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise MoneyError(f"expected an amount like '5 USD', got {text!r}")
    return Money.from_amount(match.group(1), match.group(2).upper())


def from_price(per_unit: Number, units: Number, currency: str, per: int = 1) -> Money:
    """Cost of `units` at `per_unit` money per `per` units (e.g. USD per 1,000,000 tokens)."""
    price, count = _decimal(per_unit), _decimal(units)
    if price < 0 or count < 0:
        raise MoneyError("price and units must not be negative")
    if isinstance(per, bool) or not isinstance(per, int) or per <= 0:
        raise MoneyError("`per` must be a positive int")
    cost = _CTX.divide(_CTX.multiply(price, count), per)
    return Money(_ceil_nano(_CTX.multiply(cost, NANO_PER_UNIT)), currency)


@dataclass(frozen=True, slots=True)
class FxSnapshot:
    """`rate` units of `to_currency` per 1 unit of `from_currency`, as of a date, from a source."""

    from_currency: str
    to_currency: str
    rate: Decimal
    as_of: str
    source: str

    def __post_init__(self) -> None:
        _check_currency(self.from_currency)
        _check_currency(self.to_currency)
        if not isinstance(self.rate, Decimal):
            raise TypeError("FxSnapshot.rate must be a Decimal; build it with FxSnapshot.of")
        if not self.rate.is_finite() or self.rate <= 0:
            raise MoneyError("FX rate must be a positive finite number")
        try:
            date.fromisoformat(self.as_of)
        except ValueError as exc:
            raise MoneyError(f"FX as_of must be an ISO date, got {self.as_of!r}") from exc
        if not self.source.strip():
            raise MoneyError("FX source must be named")

    @classmethod
    def of(
        cls, from_currency: str, to_currency: str, rate: Number, as_of: str, source: str
    ) -> "FxSnapshot":
        return cls(from_currency, to_currency, _decimal(rate), as_of, source)


def convert(amount: Money, fx: FxSnapshot) -> Money:
    """Convert with the snapshot's rate, rounding up to the next nano-unit."""
    if amount.currency != fx.from_currency:
        raise CurrencyMismatch(f"rate converts {fx.from_currency}, amount is {amount.currency}")
    nano = _ceil_nano(_CTX.multiply(Decimal(amount.nano), fx.rate))
    return Money(nano, fx.to_currency)


def convert_to(amount: Money, target: str, snapshots: Iterable[FxSnapshot]) -> Money:
    """Amount in `target`; same currency passes through, a missing rate is `MissingFxRate`."""
    if amount.currency == target:
        return amount
    for fx in snapshots:
        if fx.from_currency == amount.currency and fx.to_currency == target:
            return convert(amount, fx)
    raise MissingFxRate(f"no FX rate {amount.currency} to {target}")
