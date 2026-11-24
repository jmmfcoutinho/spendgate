"""A call that was sent is never unbooked: when its answer cannot be read, the full hold is charged.

An adapter that raises while reading usage or an error, a stream relay that raises, and usage too
big for the ledger all end the request at the full hold (M-13) instead of leaving it pending.
"""

from pathlib import Path

import pytest

from spendgate.core.cost import Usage
from spendgate.core.outcomes import Outcome, ReservationState
from spendgate.providers.openai import OpenAIProvider
from tests.fakes import Script
from tests.integration.harness import Stack, async_test, open_stack
from tests.integration.stream_rig import DONE, chat_chunk
from tests.integration.test_gateway_streaming import gated, open_call, settled_once


def raises(*args: object, **kwargs: object) -> object:
    raise RuntimeError("adapter bug")


async def booked_at_the_hold(s: Stack) -> None:
    held = s.observer.held[0]
    assert await s.totals() == (held.hold.nano, 0)
    (row,) = await s.ledger.get_calls(held.request_id)
    assert row.charged_nano == held.hold.nano and row.outcome is Outcome.INTERNAL_ERROR


@async_test
async def test_cr_01_usage_that_raises_after_send_is_charged_the_full_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-01: `usage` raising after a 200 answer is a 500 at the full hold."""
    monkeypatch.setattr(OpenAIProvider, "usage", raises)
    async with open_stack(tmp_path) as s:
        r = await s.chat()
        assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
        await booked_at_the_hold(s)


@async_test
async def test_cr_01_error_that_raises_after_send_is_charged_the_full_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-01: `error` raising while reading a 5xx answer is charged the full hold."""
    monkeypatch.setattr(OpenAIProvider, "error", raises)
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(status=500))
        r = await s.chat()
        assert r.status_code == 500
        await booked_at_the_hold(s)


@async_test
async def test_cr_01_streamed_usage_that_raises_is_charged_the_full_hold(tmp_path: Path) -> None:
    """CR-01: a stream relay whose usage reader raises settles once, at the full hold."""
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        up.streams[0].put(chat_chunk("a"))
        assert await call.chunk() is not None

        def boom() -> object:
            raise RuntimeError("relay bug")

        # The relay is two private layers down; reaching it is the only way to make it raise.
        s.pipeline._leases.open()[0]._live.relay.usage = boom  # type: ignore[attr-defined]
        up.streams[0].put(DONE)
        up.streams[0].end()
        await call.read_all()
        await call.finish()
        await settled_once(s, Outcome.OK_NO_USAGE, s.observer.held[0].hold.nano)


@async_test
async def test_cr_04_usage_that_does_not_fit_the_ledger_charges_the_hold_and_flags_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-04: usage too big to book is charged the hold, settled, and flagged for review."""
    monkeypatch.setattr(
        OpenAIProvider, "usage", lambda self, req, resp: Usage(input_tokens=1, output_tokens=10**17)
    )
    async with open_stack(tmp_path) as s:
        r = await s.chat()
        held = s.observer.held[0]
        assert r.status_code == 200
        assert await s.totals() == (held.hold.nano, 0)  # booked, not left pending
        (row,) = await s.ledger.get_calls(held.request_id)
        assert row.review_required_at is not None and row.charged_nano == held.hold.nano
        res = await s.ledger.get_reservation(held.request_id)
        assert res is not None and res.state is ReservationState.SETTLED
