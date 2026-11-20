"""Every row of the settlement table can be produced by the fake upstream."""

import pytest

from spendgate.core.outcomes import SETTLEMENT, Attempt, Outcome, not_processed
from tests.fakes import FakeUpstream
from tests.fakes.scenarios import FREE_STATUSES, OUTCOME_SCENARIOS, observe

pytestmark = pytest.mark.anyio

# Outcomes whose attempt reached the provider; the rest never left (unreachable) or are free.
NEVER_SENT = {Outcome.UNREACHABLE}


def test_every_settlement_row_has_a_scenario() -> None:
    assert set(OUTCOME_SCENARIOS) == set(SETTLEMENT)


@pytest.mark.parametrize("outcome", list(SETTLEMENT))
async def test_scenario_produces_its_outcome(outcome: Outcome) -> None:
    fake = FakeUpstream()
    assert await observe(fake, OUTCOME_SCENARIOS[outcome]) is outcome


@pytest.mark.parametrize("outcome", list(SETTLEMENT))
async def test_upstream_contact_matches_the_rule(outcome: Outcome) -> None:
    fake = FakeUpstream()
    await observe(fake, OUTCOME_SCENARIOS[outcome])
    assert (fake.calls == 0) is (outcome in NEVER_SENT)


async def test_only_never_sent_and_free_status_scenarios_are_not_processed() -> None:
    free = {Outcome.UNREACHABLE, Outcome.THROTTLED}
    for outcome, scenario in OUTCOME_SCENARIOS.items():
        fake = FakeUpstream()
        await observe(fake, scenario)
        status = scenario.script.status
        if fake.calls == 0:
            attempt = Attempt(sent=False)
        else:  # hangs and cuts never delivered a status the pipeline could act on
            answered = status >= 400 and not scenario.script.hang
            attempt = Attempt(sent=True, status=status if answered else None)
        assert not_processed(attempt, FREE_STATUSES) is (outcome in free), outcome
