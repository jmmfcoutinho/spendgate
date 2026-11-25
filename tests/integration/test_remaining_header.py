"""The budget-remaining header and gauge report only budgets that apply to the call.

A budget with a `model_filter` that does not match the call's catalog id takes no part in the
hold, so it must not show up as the call's tightest budget either. The room comes back with the
hold and the settlement (issue #7): a header costs no ledger read of its own.
"""

from pathlib import Path

from spendgate.core.budgets import Period
from spendgate.core.outcomes import ReservationState
from spendgate.core.sessions import SessionDefaults
from spendgate.ledger.port import BudgetRoom, BudgetTotals, MoneyLedger, Settlement
from spendgate.ledger.sqlite import SqliteLedger
from spendgate.pipeline.closing import Closer
from tests.fakes.ledger import MoneyLedgerProxy
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


class CountTotals(MoneyLedgerProxy):
    """The real ledger, counting `totals` reads (what the headers used to read, under lock)."""

    reads: list[str]

    def __init__(self, inner: SqliteLedger) -> None:
        super().__init__(inner)
        CountTotals.reads = []

    async def totals(self, scope_id: str, now: int) -> tuple[BudgetTotals, ...]:
        CountTotals.reads.append(scope_id)
        return await self.inner.totals(scope_id, now)


def counting(inner: SqliteLedger) -> MoneyLedger:
    return CountTotals(inner)


@async_test
async def test_a_call_and_a_stream_take_their_remaining_header_from_the_ledger_writes(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path, budget="10", wrap_ledger=counting) as s:
        await s.ledger.create_budget(s.scope.parent_id or "", Period.NONE, usd("50"), now=NOW_MS)
        plain = await s.chat()
        after_plain, _ = await s.totals()
        stream = await s.chat(stream=True)
        assert plain.status_code == stream.status_code == 200
        assert CountTotals.reads == []
        spent, held = await s.totals()
        assert held == 0

        def room(used: int) -> str:  # the key's 10 USD budget is tighter than the org's 50
            return f"{(usd('10') - used) / 1_000_000_000:.9f} USD"

        assert plain.headers["x-spendgate-budget-remaining"] == room(after_plain)
        assert stream.text.split("remaining=")[1].startswith(room(spent) + " scope=")


@async_test
async def test_a_session_budget_is_not_reported_as_the_remainder(tmp_path: Path) -> None:
    defaults = SessionDefaults(budget_nano=usd("0.5"))  # tighter than the key's 10 USD
    async with open_stack(tmp_path, budget="10", session_defaults=defaults) as s:
        body = {"model": "alpha", "max_tokens": 100, "messages": USER}
        r = await s.post("/v1/chat/completions", body, headers={"x-spendgate-session": "s-1"})
        assert r.status_code == 200, r.text
        spent, _ = await s.totals()
        assert r.headers["x-spendgate-budget-remaining"] == (
            f"{(usd('10') - spent) / 1_000_000_000:.9f} USD"
        )


@async_test
async def test_settlement_rooms_are_used_only_when_they_cover_every_held_budget(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path, budget="10") as s:
        closer = Closer(s.settings, s.ledger, None, s.observer, s.clock.now)  # type: ignore[arg-type]
        mine = frozenset({s.budget.id})
        room = BudgetRoom(s.budget.id, s.scope.id, usd("3"))
        settled = Settlement(True, ReservationState.SETTLED, rooms=(room,))
        covered = await closer.left_after(s.scope.id, settled, mine)
        assert covered is not None and covered[0].nano == usd("3")  # from the settlement
        # A room missing (the hold's period has ended since, the budget was disabled, or the
        # settlement was not applied): a fresh lock-free read instead, here the full 10 USD.
        for done in (Settlement(True, ReservationState.SETTLED), None):
            fresh = await closer.left_after(s.scope.id, done, mine)
            assert fresh is not None and fresh[0].nano == usd("10")
        partial = await closer.left_after(s.scope.id, settled, mine | {"another-budget"})
        assert partial is not None and partial[0].nano == usd("10")
