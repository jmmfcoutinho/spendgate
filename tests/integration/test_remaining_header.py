"""The budget-remaining header and gauge report only budgets that apply to the call.

A budget with a `model_filter` that does not match the call's catalog id takes no part in the
hold, so it must not show up as the call's tightest budget either.
"""

from pathlib import Path

from spendgate.core.budgets import Period
from tests.integration.harness import NOW_MS, Stack, async_test, open_stack, usd

USER = [{"role": "user", "content": "hi"}]


async def filtered_budget(s: Stack, cap: str, pattern: str) -> None:
    scope = s.scope.id
    await s.ledger.create_budget(scope, Period.NONE, usd(cap), now=NOW_MS, model_filter=pattern)


@async_test
async def test_a_budget_filtered_to_another_model_is_not_reported(tmp_path: Path) -> None:
    async with open_stack(tmp_path, budget="10") as s:
        await filtered_budget(s, "1", "anthropic/*")
        r = await s.chat("alpha")  # openai/gpt-6-luna: the anthropic budget does not apply
        assert r.status_code == 200, r.text
        spent, _ = await s.totals()
        expected = f"{(usd('10') - spent) / 1_000_000_000:.9f} USD"
        assert r.headers["x-spendgate-budget-remaining"] == expected
        assert s.observer.settled[0].remaining is not None
        assert s.observer.settled[0].remaining.format() == expected  # the gauge agrees


@async_test
async def test_a_matching_filtered_budget_is_reported_when_tighter(tmp_path: Path) -> None:
    async with open_stack(tmp_path, budget="10") as s:
        await filtered_budget(s, "1", "anthropic/*")
        body = {"model": "haiku", "max_tokens": 50, "messages": USER}
        r = await s.client.post("/v1/messages", json=body, headers={"x-api-key": s.secret})
        assert r.status_code == 200, r.text
        remaining = r.headers["x-spendgate-budget-remaining"]
        assert remaining.startswith("0.99") and remaining.endswith(" USD")  # the 1 USD budget


@async_test
async def test_a_stream_reports_only_applicable_budgets(tmp_path: Path) -> None:
    async with open_stack(tmp_path, budget="10") as s:
        await filtered_budget(s, "1", "anthropic/*")
        r = await s.chat("alpha", stream=True)
        assert r.status_code == 200, r.text
        remaining = r.headers["x-spendgate-budget-remaining"]  # sent before the cost exists
        assert remaining.startswith("9.") and remaining.endswith(" USD")
