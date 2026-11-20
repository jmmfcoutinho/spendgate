"""ADR-0010: `calls.http_status` is the provider's status per attempt, NULL when no answer came."""

import io
import logging
from pathlib import Path

from spendgate.core.money import Money
from spendgate.core.outcomes import Outcome
from spendgate.gateway.logs import JsonFormatter
from spendgate.gateway.metrics import Metrics
from spendgate.gateway.wiring import GatewayObserver
from tests.fakes import Script
from tests.integration.harness import async_test, open_stack
from tests.integration.stream_rig import DONE, chat_chunk, chat_usage
from tests.integration.test_admin import BASE
from tests.integration.test_admin_observer import event
from tests.integration.test_admin_support import open_admin
from tests.integration.test_gateway_streaming import SEEN, gated, open_call


async def statuses(s, response) -> list[int | None]:  # type: ignore[no-untyped-def]  # noqa: ANN001
    rows = await s.ledger.get_calls(response.headers["x-spendgate-request-id"])
    return [c.http_status for c in rows]


@async_test
async def test_a_provider_404_is_recorded_as_404(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(status=404))
        r = await s.chat()
        assert r.status_code == 502  # spendgate's answer differs from the provider's
        assert await statuses(s, r) == [404]


@async_test
async def test_a_429_failover_records_429_then_200(tmp_path: Path) -> None:
    async with open_stack(tmp_path, cooldown=1) as s:
        s.fake.enqueue(Script(status=429, retry_after="3"))
        r = await s.chat()
        assert r.status_code == 200
        assert await statuses(s, r) == [429, 200]


@async_test
async def test_a_connect_failure_records_no_status(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(refuse_connection=True))
        r = await s.chat()
        rows = await s.ledger.get_calls(r.headers["x-spendgate-request-id"])
        assert [(c.outcome, c.http_status) for c in rows] == [
            (Outcome.UNREACHABLE, None),
            (Outcome.OK, 200),
        ]


@async_test
async def test_a_stream_records_the_status_of_the_opened_response(tmp_path: Path) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        up.streams[0].put(chat_chunk("Hi"))
        up.streams[0].put(chat_usage(SEEN))
        up.streams[0].put(DONE)
        up.streams[0].end()
        await call.read_all()
        await call.finish()
        await s.pipeline.aclose()
        (row,) = await s.ledger.get_calls(s.observer.held[0].request_id)
        assert row.http_status == 200


@async_test
async def test_admin_calls_return_http_status(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        a.stack.fake.enqueue(Script(status=404))
        r = await a.stack.chat()
        rid = r.headers["x-spendgate-request-id"]
        one = await a.ok("GET", f"{BASE}/calls/{rid}")
        assert one["http_status"] == 404
        listed = await a.ok("GET", f"{BASE}/calls")
        assert [c["http_status"] for c in listed["data"]] == [404]  # type: ignore[attr-defined]


def test_call_logs_carry_http_status() -> None:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test.calls.http_status")
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.INFO)
    obs = GatewayObserver(Metrics(), logger)
    obs.on_held(None)  # type: ignore[arg-type]
    obs.on_settled(event(http_status=404, charged=Money(0, "USD")))
    assert '"http_status":404' in buf.getvalue()
