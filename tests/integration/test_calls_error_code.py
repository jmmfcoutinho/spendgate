"""ADR-0022: `calls.error_code` gets a stable code, never an exception class name."""

import sqlite3
from contextlib import closing
from pathlib import Path

from spendgate.core.failures import AttemptError, StopReason
from spendgate.core.outcomes import Outcome
from spendgate.pipeline import streaming
from tests.fakes import Script
from tests.integration.harness import async_test, open_stack
from tests.integration.stream_rig import chat_chunk
from tests.integration.test_gateway_streaming import gated, open_call


def codes(tmp_path: Path, request_id: str) -> list[tuple[str, str | None]]:
    """The stored (outcome, error_code) of each attempt, read from the ledger file itself."""
    with closing(sqlite3.connect(tmp_path / "ledger.db")) as db:
        rows = db.execute(
            "SELECT outcome, error_code FROM calls WHERE request_id = ? ORDER BY attempt",
            (request_id,),
        ).fetchall()
    return [(str(o), None if e is None else str(e)) for o, e in rows]


@async_test
async def test_a_connect_failure_is_stored_as_connect_error(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(refuse_connection=True))
        r = await s.chat()
        assert r.status_code == 200  # failed over to the second deployment
        rid = r.headers["x-spendgate-request-id"]
        assert codes(tmp_path, rid) == [
            (Outcome.UNREACHABLE, AttemptError.CONNECT_ERROR),
            (Outcome.OK, None),
        ]


@async_test
async def test_a_stream_cut_by_the_provider_is_stored_by_its_transport_code(tmp_path: Path) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        up.streams[0].put(chat_chunk("Hi"))
        up.streams[0].cut()  # the peer closes mid-body: an HTTP protocol error
        await call.read_all()
        await call.finish()
        await s.pipeline.aclose()
        rid = s.observer.held[0].request_id
        assert codes(tmp_path, rid) == [(Outcome.STREAM_BROKEN, AttemptError.REMOTE_PROTOCOL_ERROR)]


@async_test
async def test_a_client_that_goes_away_is_stored_as_client_gone(tmp_path: Path) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        up.streams[0].put(chat_chunk("Hi"))
        assert await call.chunk() is not None
        await call.disconnect()
        await s.pipeline.aclose()
        rid = s.observer.held[0].request_id
        assert codes(tmp_path, rid) == [(Outcome.CANCELLED, StopReason.CLIENT_GONE)]


@async_test
async def test_a_stream_past_its_deadline_stops_itself_as_max_duration(tmp_path: Path) -> None:
    """No heartbeat runs, so no abort: the stream's own next step sees the deadline (SR-13)."""
    async with gated(tmp_path, pipeline_kwargs={"max_request_seconds": 1}) as (s, up):
        call = await open_call(s)
        s.clock.advance(seconds=2)  # past the deadline; the 60 s lease is nowhere near expiry
        up.streams[0].put(chat_chunk("late"))
        text = (await call.read_all()).decode()
        await call.finish()
        await s.pipeline.aclose()
        assert streaming._TOO_LONG_MESSAGE in text  # the table itself is what is checked
        rid = s.observer.held[0].request_id
        assert codes(tmp_path, rid) == [(Outcome.STREAM_BROKEN, StopReason.MAX_DURATION)]
