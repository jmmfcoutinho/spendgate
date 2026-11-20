"""Money maths: exact integers, every rounding up, no mixing of currencies, no floats."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from spendgate.core.money import (
    CurrencyMismatch,
    FxSnapshot,
    MissingFxRate,
    Money,
    MoneyError,
    convert,
    convert_to,
    from_price,
    parse_amount,
)

EUR, USD = "EUR", "USD"
FX = FxSnapshot.of(USD, EUR, "0.92", "2026-11-13", "test fixture")

nanos = st.integers(min_value=0, max_value=10**15)


def test_mixing_currencies_raises_for_every_operator() -> None:
    a, b = Money(5, EUR), Money(5, USD)
    for op in (
        lambda: a + b,
        lambda: a - b,
        lambda: a < b,
        lambda: a <= b,
        lambda: a > b,
        lambda: a >= b,
    ):
        with pytest.raises(CurrencyMismatch):
            op()


def test_same_currency_arithmetic_and_order() -> None:
    a, b = Money(7, EUR), Money(3, EUR)
    assert a + b == Money(10, EUR)
    assert a - b == Money(4, EUR)
    assert b - a == Money(-4, EUR)
    assert a * 3 == Money(21, EUR)
    assert b < a and b <= a and a > b and a >= b and a >= Money(7, EUR) and a <= Money(7, EUR)


def test_money_rejects_non_money_and_non_int_operands() -> None:
    with pytest.raises(TypeError):
        Money(1, EUR) + 1  # type: ignore[operator]
    with pytest.raises(TypeError):
        Money(1, EUR) * 1.5  # type: ignore[operator]
    with pytest.raises(TypeError):
        Money(1, EUR) * True


def test_money_construction_rejects_floats_bools_and_bad_currency() -> None:
    with pytest.raises(TypeError):
        Money(1.5, EUR)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money(True, EUR)
    for code in ("eur", "EURO", "", "E1R"):
        with pytest.raises(MoneyError):
            Money(1, code)
    with pytest.raises(MoneyError):
        Money(1, 3)  # type: ignore[arg-type]


def test_from_amount_rounds_up_to_the_next_nano() -> None:
    assert Money.from_amount("0.0000000011", EUR).nano == 2
    assert Money.from_amount(Decimal("0.0000000010"), EUR).nano == 1
    assert Money.from_amount(5, EUR).nano == 5 * 10**9
    assert Money.zero(EUR).nano == 0


@pytest.mark.parametrize("bad", [0.1, True, None, [1]])
def test_floats_and_non_numbers_are_rejected_at_the_edge(bad: object) -> None:
    with pytest.raises(TypeError):
        Money.from_amount(bad, EUR)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        from_price(bad, 1, EUR)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        FxSnapshot(USD, EUR, bad, "2026-11-13", "x")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["abc", "NaN", "Infinity", ""])
def test_unparseable_or_non_finite_amounts_are_rejected(bad: str) -> None:
    with pytest.raises(MoneyError):
        Money.from_amount(bad, EUR)


def test_format_and_to_decimal() -> None:
    assert Money(12_500_000_000, EUR).format() == "12.500000000 EUR"
    assert Money(1, USD).format() == "0.000000001 USD"
    assert Money(-1_500_000_000, USD).format() == "-1.500000000 USD"
    assert Money(1_500_000_000, EUR).to_decimal() == Decimal("1.5")


def test_parse_amount_quick_start_form() -> None:
    assert parse_amount("5 USD") == Money(5 * 10**9, USD)
    assert parse_amount(" 0.25 eur ") == Money(250_000_000, EUR)
    for bad in ("5", "USD 5", "-5 USD", "5 US", "5USD", "1e3 USD"):
        with pytest.raises(MoneyError):
            parse_amount(bad)
    with pytest.raises(MoneyError):
        parse_amount(5)  # type: ignore[arg-type]


def test_from_price_per_million_tokens_rounds_up() -> None:
    # 0.12 USD per 1M tokens, 1 token = 0.00000012 USD = 120 nano exactly.
    assert from_price("0.12", 1, USD, per=1_000_000).nano == 120
    # 1 token at 0.0000001 per 3 units = 33.33.. nano -> 34.
    assert from_price("0.0000001", 1, USD, per=3).nano == 34
    assert from_price("0.12", 0, USD, per=1_000_000).nano == 0
    assert from_price(2, Decimal("2.5"), USD).nano == 5 * 10**9


def test_from_price_rejects_negatives_and_bad_per() -> None:
    with pytest.raises(MoneyError):
        from_price("-1", 1, USD)
    with pytest.raises(MoneyError):
        from_price("1", -1, USD)
    for per in (0, -1, True, 1.5):
        with pytest.raises(MoneyError):
            from_price("1", 1, USD, per=per)  # type: ignore[arg-type]


def test_fx_snapshot_validation() -> None:
    with pytest.raises(MoneyError):
        FxSnapshot.of(USD, EUR, "0", "2026-11-13", "x")
    with pytest.raises(MoneyError):
        FxSnapshot.of(USD, EUR, "-1", "2026-11-13", "x")
    with pytest.raises(MoneyError):
        FxSnapshot.of(USD, EUR, "0.9", "13/11/2026", "x")
    with pytest.raises(MoneyError):
        FxSnapshot.of(USD, EUR, "0.9", "2026-11-13", "  ")
    with pytest.raises(MoneyError):
        FxSnapshot.of("usd", EUR, "0.9", "2026-11-13", "x")
    assert FX.rate == Decimal("0.92") and FX.as_of == "2026-11-13" and FX.source


def test_convert_rounds_up_and_checks_the_pair() -> None:
    assert convert(Money(1, USD), FX) == Money(1, EUR)  # 0.92 nano -> 1
    assert convert(Money(100, USD), FX) == Money(92, EUR)
    assert convert(Money(101, USD), FX) == Money(93, EUR)  # 92.92 -> 93
    with pytest.raises(CurrencyMismatch):
        convert(Money(1, EUR), FX)


def test_m09_missing_fx_rate_is_refused_never_defaulted() -> None:
    assert convert_to(Money(5, EUR), EUR, []) == Money(5, EUR)
    assert convert_to(Money(100, USD), EUR, [FX]) == Money(92, EUR)
    with pytest.raises(MissingFxRate):
        convert_to(Money(100, USD), "GBP", [FX])
    with pytest.raises(MissingFxRate):
        convert_to(Money(100, EUR), USD, [FX])  # the inverse is not assumed


@given(st.lists(nanos, max_size=50))
def test_sums_never_drift(values: list[int]) -> None:
    total = Money.zero(EUR)
    for v in values:
        total = total + Money(v, EUR)
    assert total.nano == sum(values)
    for v in values:
        total = total - Money(v, EUR)
    assert total == Money.zero(EUR)


@given(st.decimals(min_value=0, max_value=10**6, places=12, allow_nan=False, allow_infinity=False))
def test_from_amount_never_rounds_down(amount: Decimal) -> None:
    nano = Money.from_amount(amount, EUR).nano
    assert Decimal(nano) >= amount * 10**9
    assert Decimal(nano) - amount * 10**9 < 1


@given(
    price=st.decimals(
        min_value=0, max_value=1000, places=10, allow_nan=False, allow_infinity=False
    ),
    units=st.integers(min_value=0, max_value=10**9),
    per=st.integers(min_value=1, max_value=10**7),
)
def test_from_price_never_rounds_down(price: Decimal, units: int, per: int) -> None:
    nano = from_price(price, units, USD, per=per).nano
    exact = price * units * 10**9 / per
    assert Decimal(nano) >= exact - Decimal("1e-40")
    assert Decimal(nano) - exact < 1 + Decimal("1e-40")


@given(
    nano=nanos,
    rate=st.decimals(min_value="0.0001", max_value=1000, places=8, allow_nan=False),
)
def test_convert_never_rounds_down(nano: int, rate: Decimal) -> None:
    fx = FxSnapshot.of(USD, EUR, rate, "2026-11-13", "hypothesis")
    out = convert(Money(nano, USD), fx).nano
    assert Decimal(out) >= Decimal(nano) * rate
    assert Decimal(out) - Decimal(nano) * rate < 1
