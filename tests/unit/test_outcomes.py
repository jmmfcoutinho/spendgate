"""Settlement table, `not_processed` and provider rule config (rules M-03, M-10, M-13)."""

import pytest

from spendgate.core.money import CurrencyMismatch, Money
from spendgate.core.outcomes import (
    SETTLEMENT,
    Attempt,
    Basis,
    Outcome,
    ProviderRuleError,
    ReservationState,
    charge,
    not_processed,
    outcome_for_status,
    provider_free_statuses,
    validate_free_statuses,
)

HOLD = Money(1_000, "EUR")
ZERO = Money.zero("EUR")
USAGE = Money(400, "EUR")
OVER = Money(1_800, "EUR")
FREE = frozenset({429})
ANTHROPIC_FREE = frozenset({429, 529})


def test_outcome_enum_matches_the_data_model_exactly() -> None:
    assert [o.value for o in Outcome] == [
        "ok", "ok_no_usage", "client_error", "throttled", "timeout", "upstream_5xx",
        "unreachable", "cancelled", "stream_broken", "internal_error", "expired",
    ]  # fmt: skip
    assert {s.value for s in ReservationState} == {"pending", "settled", "released", "expired"}


def test_every_outcome_has_exactly_one_rule_with_a_unique_id() -> None:
    assert set(SETTLEMENT) == set(Outcome)
    ids = [rule.rule_id for rule in SETTLEMENT.values()]
    assert len(ids) == len(set(ids))


# --- one test per settlement row ------------------------------------------------------------


def test_s01_ok_books_actual_usage() -> None:
    assert charge(Outcome.OK, USAGE, HOLD) == USAGE


def test_s01_ok_without_a_usable_usage_figure_is_charged_the_full_hold() -> None:
    assert charge(Outcome.OK, None, HOLD) == HOLD


def test_s02_ok_no_usage_is_charged_the_full_hold() -> None:
    assert charge(Outcome.OK_NO_USAGE, None, HOLD) == HOLD


def test_s03_client_error_without_usage_is_free() -> None:
    assert charge(Outcome.CLIENT_ERROR, None, HOLD) == ZERO


def test_s04_throttled_free_status_costs_nothing() -> None:
    assert charge(Outcome.THROTTLED, None, HOLD) == ZERO


def test_s05_timeout_after_send_is_charged_the_full_hold() -> None:
    assert charge(Outcome.TIMEOUT, None, HOLD) == HOLD


def test_s06_upstream_5xx_after_send_is_charged_the_full_hold() -> None:
    assert charge(Outcome.UPSTREAM_5XX, None, HOLD) == HOLD


def test_s07_unreachable_never_sent_is_free() -> None:
    assert charge(Outcome.UNREACHABLE, None, HOLD) == ZERO


def test_m13_cancel_after_send_is_charged_the_full_hold() -> None:
    assert charge(Outcome.CANCELLED, None, HOLD) == HOLD


def test_m13_cancel_after_send_with_usage_seen_is_charged_the_usage() -> None:
    assert charge(Outcome.CANCELLED, USAGE, HOLD) == USAGE


def test_s09_stream_broken_is_charged_the_full_hold() -> None:
    assert charge(Outcome.STREAM_BROKEN, None, HOLD) == HOLD


def test_s10_internal_error_after_contact_is_charged_the_full_hold() -> None:
    assert charge(Outcome.INTERNAL_ERROR, None, HOLD) == HOLD


def test_s10_internal_error_before_contact_is_free_when_not_processed() -> None:
    assert charge(Outcome.INTERNAL_ERROR, None, HOLD, not_processed=True) == ZERO


def test_s11_expired_sweeper_charge_is_the_full_hold_even_if_usage_is_known() -> None:
    assert charge(Outcome.EXPIRED, None, HOLD) == HOLD
    assert charge(Outcome.EXPIRED, USAGE, HOLD) == HOLD


def test_m03_reported_usage_is_booked_never_clamped_to_the_hold() -> None:
    assert charge(Outcome.OK, OVER, HOLD) == OVER
    assert charge(Outcome.UPSTREAM_5XX, OVER, HOLD) == OVER


def test_usage_is_booked_even_for_a_free_basis_outcome() -> None:
    assert charge(Outcome.CLIENT_ERROR, USAGE, HOLD) == USAGE


def test_charge_refuses_mixed_currencies() -> None:
    with pytest.raises(CurrencyMismatch):
        charge(Outcome.OK, Money(1, "USD"), HOLD)


def test_the_basis_of_each_row_is_what_the_doc_table_says() -> None:
    free = {o for o, r in SETTLEMENT.items() if r.basis is Basis.USAGE_OR_ZERO}
    assert free == {Outcome.CLIENT_ERROR, Outcome.THROTTLED, Outcome.UNREACHABLE}
    assert SETTLEMENT[Outcome.EXPIRED].basis is Basis.FULL_HOLD
    charged = set(Outcome) - free - {Outcome.EXPIRED}
    assert all(SETTLEMENT[o].basis is Basis.USAGE_OR_HOLD for o in charged)


# --- not_processed truth tables -------------------------------------------------------------


@pytest.mark.parametrize(
    ("free", "status", "expected"),
    [
        (FREE, 429, True),
        (FREE, 529, False),  # 529 is free only for anthropic
        (FREE, 500, False),
        (FREE, 503, False),
        (FREE, 408, False),
        (FREE, 400, False),  # a 4xx is rejected, not "not processed": no failover
        (FREE, 401, False),
        (FREE, 200, False),
        (ANTHROPIC_FREE, 429, True),
        (ANTHROPIC_FREE, 529, True),
        (ANTHROPIC_FREE, 500, False),
        (frozenset[int](), 429, False),
    ],
)
def test_n2_not_processed_for_a_sent_attempt_depends_on_free_statuses(
    free: frozenset[int], status: int, expected: bool
) -> None:
    assert not_processed(Attempt(sent=True, status=status), free) is expected


def test_n2_not_processed_when_never_sent_regardless_of_free_statuses() -> None:
    assert not_processed(Attempt(sent=False), FREE)
    assert not_processed(Attempt(sent=False), frozenset())


def test_n2_sent_without_an_answer_is_processed() -> None:
    # read timeout, write error, stream cut: the request left, so it may have run.
    assert not not_processed(Attempt(sent=True), FREE)
    assert not not_processed(Attempt(sent=True, status=None), ANTHROPIC_FREE)


def test_a_never_sent_attempt_cannot_carry_a_status() -> None:
    with pytest.raises(ValueError, match="never sent"):
        Attempt(sent=False, status=429)


@pytest.mark.parametrize(
    ("status", "free", "outcome"),
    [
        (429, FREE, Outcome.THROTTLED),
        (529, ANTHROPIC_FREE, Outcome.THROTTLED),
        (529, FREE, Outcome.UPSTREAM_5XX),
        (500, FREE, Outcome.UPSTREAM_5XX),
        (503, FREE, Outcome.UPSTREAM_5XX),
        (408, FREE, Outcome.UPSTREAM_5XX),
        (400, FREE, Outcome.CLIENT_ERROR),
        (404, FREE, Outcome.CLIENT_ERROR),
        (402, FREE, Outcome.CLIENT_ERROR),
    ],
)
def test_outcome_for_status_table(status: int, free: frozenset[int], outcome: Outcome) -> None:
    assert outcome_for_status(status, free) is outcome


def test_outcome_for_status_rejects_non_errors() -> None:
    with pytest.raises(ValueError, match=">= 400"):
        outcome_for_status(200, FREE)


def test_a_not_processed_attempt_costs_zero_through_charge() -> None:
    # The pipeline path: status -> outcome -> not_processed -> charge.
    attempt = Attempt(sent=True, status=429)
    outcome = outcome_for_status(429, FREE)
    assert charge(outcome, None, HOLD, not_processed=not_processed(attempt, FREE)) == ZERO


# --- M-10: rule config cannot fail open ------------------------------------------------------


def test_m10_free_statuses_outside_429_529_are_rejected() -> None:
    for bad in ([500], [429, 500], [200], [0], [-429]):
        with pytest.raises(ProviderRuleError):
            validate_free_statuses(bad)
    for bad_type in ([True], ["429"], [429.0]):
        with pytest.raises(ProviderRuleError):
            validate_free_statuses(bad_type)  # type: ignore[arg-type]


def test_m10_free_statuses_subsets_are_accepted() -> None:
    assert validate_free_statuses([]) == frozenset()
    assert validate_free_statuses([429, 429]) == {429}
    assert validate_free_statuses((529, 429)) == {429, 529}


def test_m10_default_free_statuses_per_provider() -> None:
    assert provider_free_statuses("openai") == {429}
    assert provider_free_statuses("anthropic") == {429, 529}
    assert provider_free_statuses("openai", {}) == {429}


def test_m10_charge_zero_and_unknown_rules_are_rejected() -> None:
    with pytest.raises(ProviderRuleError, match="charge_zero"):
        provider_free_statuses("openai", {"charge_zero": [500]})
    with pytest.raises(ProviderRuleError):
        provider_free_statuses("openai", {"free_statuses": [429], "other": 1})


def test_m10_explicit_free_statuses_override_the_default() -> None:
    assert provider_free_statuses("anthropic", {"free_statuses": [429]}) == {429}
    assert provider_free_statuses("openai", {"free_statuses": [429, 529]}) == {429, 529}
    with pytest.raises(ProviderRuleError):
        provider_free_statuses("openai", {"free_statuses": [500]})
    with pytest.raises(ProviderRuleError, match="list"):
        provider_free_statuses("openai", {"free_statuses": "429"})
