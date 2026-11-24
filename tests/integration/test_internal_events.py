"""Unexpected exceptions leave one redacted trace on the JSON log (`spendgate.internal`).

A catch-all keeps money handling correct when an adapter or the pipeline has a bug, but it used to
answer `500 internal_error` with no log record at all. Each catch-all now emits one event with the
exception class, the frames as `module:function:line` and the catch-all's tag. The exception's
message never reaches the log: it can quote a body or a secret.
"""

import io
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import NoReturn

import pytest
from starlette.requests import Request
from starlette.responses import Response

from spendgate.core.cost import Usage
from spendgate.core.errors import Refusal
from spendgate.core.outcomes import ReservationState
from spendgate.gateway.admin_http import AdminContext, Call, guarded
from spendgate.gateway.logs import INTERNAL_EVENT, configure_json_logging
from spendgate.gateway.metrics import Metrics
from spendgate.gateway.wiring import GatewayObserver
from spendgate.ledger.key_types import AdminScope
from spendgate.ledger.port import LedgerPort, LedgerUnavailable
from spendgate.ledger.sqlite import SqliteLedger
from spendgate.providers.anthropic import AnthropicProvider
from spendgate.providers.bedrock import Bedrock
from spendgate.providers.google import GoogleProvider
from spendgate.providers.openai import OpenAIProvider
from spendgate.providers.port import (
    CleanRequest,
    Deployment,
    Provider,
    SseLimits,
    StreamRelay,
    UpstreamError,
    UpstreamRequest,
    UpstreamResponse,
)
from tests.fakes import Script
from tests.integration.harness import AWS_EXAMPLE, Stack, async_test, open_stack

SECRET = "sk-fake-" + "0" * 24  # pragma: allowlist secret
MARKER = "MESSAGE-TEXT-MARKER"  # not secret-shaped: shows the message itself is never read
STREAM = {"stream": True, "stream_options": {"include_usage": True}}


def boom() -> NoReturn:
    raise KeyError(f"{MARKER} {SECRET}")


class Broken(OpenAIProvider):
    """An OpenAI adapter with a programming error in one method."""

    def __init__(self, method: str) -> None:
        super().__init__()
        self.method = method

    def translate(self, req: CleanRequest, deployment: Deployment) -> UpstreamRequest:
        if self.method == "translate":
            boom()
        return super().translate(req, deployment)

    def validate(self, req: CleanRequest) -> Refusal | None:
        if self.method == "validate":
            boom()
        return super().validate(req)

    def usage(self, req: CleanRequest, response: UpstreamResponse) -> Usage | None:
        if self.method == "usage":
            boom()
        return super().usage(req, response)

    def response(self, req: CleanRequest, resp: UpstreamResponse) -> UpstreamResponse:
        if self.method == "response":
            boom()
        return super().response(req, resp)

    def error(self, response: UpstreamResponse) -> UpstreamError:
        if self.method == "error":
            boom()
        return super().error(response)

    def relay(self, req: CleanRequest, limits: SseLimits = SseLimits()) -> StreamRelay:  # noqa: B008
        if self.method == "relay":
            boom()
        inner = super().relay(req, limits)
        return BrokenRelay(inner, self.method) if self.method.startswith("stream_") else inner


class BrokenRelay:
    def __init__(self, inner: StreamRelay, method: str) -> None:
        self._inner, self._method = inner, method

    def feed(self, chunk: bytes) -> bytes:
        if self._method == "stream_feed":
            boom()
        return self._inner.feed(chunk)

    def close(self) -> bytes:
        return self._inner.close()

    def usage(self) -> Usage | None:
        if self._method == "stream_usage":
            boom()
        return self._inner.usage()


def providers(openai: Provider) -> dict[str, Provider]:
    return {
        "openai": openai, "anthropic": AnthropicProvider(), "bedrock": Bedrock(lambda: AWS_EXAMPLE),
        "google": GoogleProvider(),
    }  # fmt: skip


@contextmanager
def json_log() -> Iterator[io.StringIO]:
    sink = io.StringIO()
    logger = configure_json_logging(logging.StreamHandler(sink))
    try:
        yield sink
    finally:
        logger.handlers = []


def internal_lines(sink: io.StringIO) -> list[dict[str, object]]:
    lines = [json.loads(line) for line in sink.getvalue().splitlines() if line]
    return [line for line in lines if line.get("event") == INTERNAL_EVENT]


@asynccontextmanager
async def broken_stack(
    tmp_path: Path, method: str, wrap_ledger: Callable[[SqliteLedger], LedgerPort] | None = None
) -> AsyncIterator[tuple[Stack, io.StringIO]]:
    with json_log() as sink:
        observer = GatewayObserver(Metrics(), logging.getLogger("spendgate.call"))
        extra = {"observer": observer, "providers": providers(Broken(method))}
        rig = open_stack(tmp_path, attempts=1, pipeline_kwargs=extra, wrap_ledger=wrap_ledger)
        async with rig as s:
            yield s, sink


def assert_redacted(sink: io.StringIO) -> None:
    text = sink.getvalue()
    assert MARKER not in text and SECRET not in text and "Traceback" not in text


@async_test
async def test_an_adapter_keyerror_leaves_exactly_one_redacted_internal_event(
    tmp_path: Path,
) -> None:
    async with broken_stack(tmp_path, "usage") as (s, sink):
        r = await s.chat()
        assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
        events = internal_lines(sink)
        assert len(events) == 1
        event = events[0]
        assert event["error_class"] == "KeyError" and event["where"] == "routing.usage"
        assert event["request_id"] == r.headers["x-spendgate-request-id"]
        assert event["level"] == "ERROR" and event["charged"] is True  # charged at the full hold
        frames = str(event["frames"]).split(" > ")
        assert any(f.startswith("spendgate.pipeline.routing:Forwarder._answered:") for f in frames)
        assert frames[-1].startswith(f"{__name__}:boom:")  # the innermost frame is the bug
        assert set(event) == {
            "event", "request_id", "where", "error_class", "frames", "charged", "level", "ts",
        }  # fmt: skip
        assert_redacted(sink)


@async_test
async def test_a_secret_in_an_exception_message_never_reaches_the_logs(tmp_path: Path) -> None:
    async with broken_stack(tmp_path, "response") as (s, sink):
        await s.chat()
        await s.chat("alpha", **STREAM)
        assert len(internal_lines(sink)) == 1  # the stream does not call response()
        assert_redacted(sink)
        assert s.secret not in sink.getvalue()


@async_test
async def test_a_charged_answer_that_cannot_be_converted_says_it_was_charged(
    tmp_path: Path,
) -> None:
    """The provider answered and the call was charged, then the client got a 500: visible."""
    async with broken_stack(tmp_path, "response") as (s, sink):
        r = await s.chat()
        assert r.status_code == 500
        rid = r.headers["x-spendgate-request-id"]
        (event,) = internal_lines(sink)
        assert event["where"] == "routing.response" and event["charged"] is True
        assert event["request_id"] == rid
        row = await s.ledger.get_reservation(rid)
        assert row is not None and row.state is ReservationState.SETTLED
        assert (await s.totals())[0] > 0  # unchanged money behaviour: the answer is charged


class SettleDown:
    """A ledger whose `settle` fails: the close is not booked and the sweeper charges the hold."""

    def __init__(self, inner: LedgerPort) -> None:
        self._inner = inner

    async def settle(self, *args: object) -> object:
        raise LedgerUnavailable("gone")

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


@async_test
async def test_a_charged_call_whose_settle_the_ledger_did_not_take_still_reads_charged(
    tmp_path: Path,
) -> None:
    async with broken_stack(tmp_path, "response", SettleDown) as (s, sink):  # type: ignore[arg-type]
        r = await s.chat()
        assert r.status_code == 500
        (event,) = internal_lines(sink)
        assert event["where"] == "routing.response" and event["charged"] is True
        assert (await s.totals())[1] > 0  # still held: the sweeper charges it in full


@pytest.mark.parametrize(
    ("method", "where", "charged", "status"),
    [
        ("translate", "routing.translate", False, 500),  # never sent: released
        ("error", "routing.error", True, 500),  # unknown what the provider did: full hold
        ("validate", "steps.prepare", False, 400),  # nothing held yet: refused
    ],
)
@async_test
async def test_each_buffered_catch_all_reports_its_tag(
    tmp_path: Path, method: str, where: str, charged: bool, status: int
) -> None:
    async with broken_stack(tmp_path, method) as (s, sink):
        s.fake.enqueue(Script(status=500))  # only `error` reads it
        r = await s.chat()
        assert r.status_code == status
        (event,) = internal_lines(sink)
        assert (event["where"], event["charged"], event["error_class"]) == (
            where,
            charged,
            "KeyError",
        )
        assert_redacted(sink)


@pytest.mark.parametrize(
    ("method", "where"),
    [("relay", "routing.relay"), ("stream_feed", "stream.relay"), ("stream_usage", "stream.usage")],
)
@async_test
async def test_each_stream_catch_all_reports_its_tag_once(
    tmp_path: Path, method: str, where: str
) -> None:
    async with broken_stack(tmp_path, method) as (s, sink):
        r = await s.chat("alpha", **STREAM)
        assert r.status_code in {200, 500}
        await s.pipeline.aclose()
        (event,) = internal_lines(sink)
        assert event["where"] == where and event["charged"] is True  # sent: charged
        assert event["request_id"] == r.headers["x-spendgate-request-id"]
        assert_redacted(sink)


@async_test
async def test_a_pipeline_bug_outside_the_adapters_is_reported_too(tmp_path: Path) -> None:
    async with broken_stack(tmp_path, "none") as (s, sink):
        s.pipeline._forwarder.run = lambda job, book: boom()  # type: ignore[method-assign]
        r = await s.chat()
        assert r.status_code == 500
        (event,) = internal_lines(sink)
        assert event["where"] == "pipeline.forward" and event["charged"] is False  # released
        assert_redacted(sink)


@async_test
async def test_a_bug_before_forwarding_is_reported_by_the_outer_catch_all(tmp_path: Path) -> None:
    async with broken_stack(tmp_path, "none") as (s, sink):
        s.pipeline._router.targets = lambda alias: boom()  # type: ignore[method-assign]
        r = await s.chat()
        assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
        (event,) = internal_lines(sink)
        assert event["where"] == "pipeline.handle" and event["charged"] is False  # not known
        assert event["request_id"] == r.headers["x-spendgate-request-id"]
        assert_redacted(sink)


@async_test
async def test_an_admin_handler_bug_is_reported_with_its_request_id() -> None:
    async def handler(call: Call) -> Response:
        boom()

    ctx = AdminContext(None, None, None, lambda: 0, 0, trusted_actor="test")  # type: ignore[arg-type]
    view = guarded(ctx, AdminScope.USAGE_READ, handler)
    with json_log() as sink:
        response = await view(Request({"type": "http", "method": "GET", "headers": []}))
        assert response.status_code == 500
        (event,) = internal_lines(sink)
        assert event["where"] == "admin.handler" and event["error_class"] == "KeyError"
        assert str(event["request_id"]).startswith("sg_adm_")
        assert_redacted(sink)
