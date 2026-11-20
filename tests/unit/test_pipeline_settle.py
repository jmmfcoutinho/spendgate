"""Step 9: one release or settle per request, charged only through `core.outcomes.charge`
(M-02, M-13)."""

from dataclasses import replace
from importlib import resources

import yaml

from spendgate.core.catalog import Catalog, parse
from spendgate.core.cost import Usage
from spendgate.core.money import Money
from spendgate.core.outcomes import Outcome
from spendgate.ledger.port import AttemptRecord
from spendgate.pipeline.settle import (
    Book,
    Flight,
    Pricing,
    decide,
    fx_rate_nano,
    ledger_usage,
    read_usage,
)
from tests.integration.harness import NOW_MS

HOLD = Money(1_000_000, "USD")
NOW = NOW_MS


def catalog() -> Catalog:
    text = (resources.files("spendgate.catalog") / "prices.yaml").read_text(encoding="utf-8")
    return parse([yaml.safe_load(text)], "0" * 64, ledger_currency="USD")


def book() -> Book:
    entry = catalog().lookup("openai", "gpt-6-luna", NOW)
    return Book(Pricing(entry, 10**9, HOLD), "alpha/0")


def record(n: int, outcome: Outcome, *, np: bool, usage_cost: int | None = None) -> AttemptRecord:
    return AttemptRecord(n, "openai", f"alpha/{n - 1}", "p", outcome, 0, NOW, NOW, np,
                         usage_cost_nano=usage_cost)  # fmt: skip


def test_no_attempt_at_all_releases_with_no_calls_row() -> None:
    b = book()
    d = decide(b, None, NOW)
    assert d.release and d.charge.nano == 0 and d.attempts == ()  # the ledger takes `()`
    assert d.outcome is Outcome.INTERNAL_ERROR and d.deployment == "alpha/0"
    b.idle_outcome = Outcome.THROTTLED
    assert decide(b, None, NOW).outcome is Outcome.THROTTLED
    assert decide(b, Outcome.CANCELLED, NOW).outcome is Outcome.CANCELLED  # a cancel names itself


def test_every_attempt_not_processed_releases() -> None:
    b = book()
    b.attempts += [record(1, Outcome.THROTTLED, np=True), record(2, Outcome.UNREACHABLE, np=True)]
    d = decide(b, None, NOW)
    assert d.release and d.charge.nano == 0 and d.outcome is Outcome.UNREACHABLE
    assert all(a.charged_nano == 0 for a in d.attempts)


def test_failover_then_success_charges_only_the_last_attempt() -> None:
    b = book()
    b.attempts += [
        record(1, Outcome.THROTTLED, np=True),
        record(2, Outcome.OK, np=False, usage_cost=300),
    ]
    b.actual = Money(300, "USD")
    d = decide(b, None, NOW)
    assert not d.release and d.charge.nano == 300
    assert [a.charged_nano for a in d.attempts] == [0, 300]  # per-attempt charges sum to the settle


def test_unreadable_usage_charges_the_full_hold() -> None:
    b = book()
    b.attempts.append(record(1, Outcome.OK_NO_USAGE, np=False))
    assert decide(b, None, NOW).charge == HOLD


def test_usage_above_the_hold_is_booked_not_clamped() -> None:  # M-03
    b = book()
    b.attempts.append(record(1, Outcome.OK, np=False, usage_cost=5_000_000))
    b.actual = Money(5_000_000, "USD")
    assert decide(b, None, NOW).charge.nano == 5_000_000


def test_provider_rejection_is_free_and_5xx_costs_the_hold() -> None:
    for outcome, cost in ((Outcome.CLIENT_ERROR, 0), (Outcome.UPSTREAM_5XX, HOLD.nano),
                          (Outcome.TIMEOUT, HOLD.nano)):  # fmt: skip
        b = book()
        b.attempts.append(record(1, outcome, np=False))
        d = decide(b, None, NOW)
        assert not d.release and d.charge.nano == cost, outcome


def test_cancel_after_send_is_the_full_hold_before_send_a_release() -> None:  # M-13, T-MON-11
    b = book()
    b.attempts.append(record(1, Outcome.THROTTLED, np=True))
    b.flight = Flight(2, "openai", "alpha/1", NOW)  # the second request is on the wire
    d = decide(b, Outcome.CANCELLED, NOW)
    assert not d.release and d.charge == HOLD and d.outcome is Outcome.CANCELLED
    assert [a.charged_nano for a in d.attempts] == [0, HOLD.nano]
    b.flight = None  # same book, but nothing was ever sent after the first free attempt
    assert decide(b, Outcome.CANCELLED, NOW).release


def test_gateway_fault_after_send_is_charged() -> None:
    b = book()
    b.flight = Flight(1, "openai", "alpha/0", NOW)
    d = decide(b, Outcome.INTERNAL_ERROR, NOW)
    assert d.charge == HOLD and d.attempts[0].error_code == "internal_error"


def test_usage_pricing_and_conversion() -> None:
    cat = catalog()
    entry = cat.lookup("openai", "gpt-6-luna", NOW)
    read = read_usage(cat, entry, Usage(input_tokens=10, output_tokens=5))
    assert read.native == read.cost == Money(3500, "USD")
    assert (
        read_usage(cat, entry, Usage(characters=5)).cost is None
    )  # an unpriced meter: caller holds
    assert fx_rate_nano(cat, "USD") == 10**9
    mapped = ledger_usage(
        Usage(input_tokens=3, output_tokens=2, cached_input_tokens=1, characters=4)
    )
    assert (mapped.tokens_in, mapped.tokens_out, mapped.tokens_cached, mapped.characters) == (
        3,
        2,
        1,
        4,
    )
    assert replace(record(1, Outcome.OK, np=False), charged_nano=1).charged_nano == 1


def test_cr_04_usage_that_does_not_fit_int64_is_unreadable_not_an_exception() -> None:
    cat = catalog()
    entry = cat.lookup("openai", "gpt-6-luna", NOW)
    huge = read_usage(cat, entry, Usage(input_tokens=1, output_tokens=10**17))
    assert huge.absurd and huge.cost is None and huge.usage is None  # the hold is charged
    beyond = read_usage(cat, entry, Usage(output_tokens=2**70))
    assert beyond.absurd and beyond.cost is None
