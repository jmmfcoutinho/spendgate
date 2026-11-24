"""Library mode through real httpx clients: sync, async, in a running loop, sessions, streams."""

import asyncio
import logging
from pathlib import Path

import httpx
import pytest

from spendgate.config import Settings
from spendgate.core.money import Money
from spendgate.local import runtime, session, transport
from spendgate.local.runtime import Call, Runtime
from spendgate.pipeline.adapters import build_providers
from spendgate.pipeline.model import Reply
from spendgate.providers.openai import OpenAIProvider
from spendgate.providers.port import CleanRequest, Provider, UpstreamResponse
from tests.fakes import FakeUpstream
from tests.integration.test_local_support import (
    BODY,
    CHAT,
    error_code,
    fake_client,
    now,
    settings_in,
)

BASE = "http://spendgate.local"


def sync_client(tmp_path: Path, fake: FakeUpstream, cap: str = "10 USD") -> httpx.Client:
    t = transport(
        settings_in(tmp_path), ledger_path=tmp_path / "local.db", cap=cap,
        client=fake_client(fake), now_ms=now,
    )  # fmt: skip
    return httpx.Client(transport=t, base_url=BASE)


def test_a_call_is_priced_charged_and_reported_in_headers(tmp_path: Path) -> None:
    fake = FakeUpstream()
    with sync_client(tmp_path, fake) as client:
        reply = client.post(CHAT, json=BODY)
    assert reply.status_code == 200
    cost = Money.from_amount(reply.headers["x-spendgate-cost"].split()[0], "USD")
    left = reply.headers["x-spendgate-budget-remaining"]
    assert 0 < cost.nano < Money.from_amount("0.001", "USD").nano
    assert left == (Money.from_amount("10", "USD") - cost).format()
    assert reply.headers["x-spendgate-budget-scope"] == "org:local"
    assert fake.calls == 1


def test_the_callers_key_never_reaches_the_provider(tmp_path: Path) -> None:
    fake = FakeUpstream()
    with sync_client(tmp_path, fake) as client:
        client.post(CHAT, json=BODY, headers={"Authorization": "Bearer sk-callers-own-key"})
    sent = fake.requests[0].headers["authorization"]
    assert sent == "Bearer sk-test-openai-not-real"  # pragma: allowlist secret


def test_over_budget_is_refused_before_the_provider(tmp_path: Path) -> None:
    fake = FakeUpstream()
    with sync_client(tmp_path, fake, cap="0.000001 USD") as client:
        reply = client.post(CHAT, json=BODY)
    assert reply.status_code == 402
    assert error_code(reply) == "budget_exhausted"
    fake.assert_no_contact()


def test_refusal_with_room_left_names_the_worst_case_hold_in_library_mode(
    tmp_path: Path,
) -> None:
    fake = FakeUpstream()
    with sync_client(tmp_path, fake, cap="0.000001 USD") as client:
        reply = client.post(CHAT, json=BODY)
    error = reply.json()["error"]
    assert reply.status_code == 402 and error["code"] == "budget_exhausted"
    assert error["details"]["remaining"] == "0.000001000 USD"
    assert f"worst-case hold for this call ({error['details']['hold']})" in error["message"]
    assert "max_tokens" in error["message"] and "is exhausted" not in error["message"]
    fake.assert_no_contact()


def test_refusal_with_nothing_left_keeps_the_exhausted_message_in_library_mode(
    tmp_path: Path,
) -> None:
    fake = FakeUpstream()
    with sync_client(tmp_path, fake, cap="0 USD") as client:
        reply = client.post(CHAT, json=BODY)
    error = reply.json()["error"]
    assert reply.status_code == 402 and error["code"] == "budget_exhausted"
    assert error["message"] == "Budget for org:local is exhausted."
    assert error["details"]["remaining"] == "0.000000000 USD"
    fake.assert_no_contact()


def test_the_sync_transport_works_inside_a_running_event_loop(tmp_path: Path) -> None:
    """The notebook case: asyncio.run stands in for Jupyter's already-running loop."""
    fake = FakeUpstream()

    async def notebook_cell() -> int:
        with sync_client(tmp_path, fake) as client:
            return client.post(CHAT, json=BODY).status_code

    assert asyncio.run(notebook_cell()) == 200


def test_the_async_transport_runs_on_the_callers_loop(tmp_path: Path) -> None:
    fake = FakeUpstream()

    async def main() -> tuple[int, int]:
        t = transport(
            settings_in(tmp_path), ledger_path=tmp_path / "local.db", cap="10 USD",
            async_=True, client=fake_client(fake), now_ms=now,
        )  # fmt: skip
        async with httpx.AsyncClient(transport=t, base_url=BASE) as client:
            both = await asyncio.gather(client.post(CHAT, json=BODY), client.post(CHAT, json=BODY))
        return both[0].status_code, both[1].status_code

    assert asyncio.run(main()) == (200, 200)


def test_models_and_unknown_paths(tmp_path: Path) -> None:
    with sync_client(tmp_path, FakeUpstream()) as client:
        listed = client.get("/v1/models")
        one = client.get("/v1/models/alpha")
        missing = client.get("/v1/nothing")
        wrong_verb = client.get(CHAT)
    assert listed.status_code == one.status_code == 200
    assert {m["id"] for m in listed.json()["data"]} >= {"alpha", "haiku"}
    assert missing.status_code == wrong_verb.status_code == 404


def test_a_transport_without_a_budget_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no budget"):
        transport(settings_in(tmp_path), ledger_path=tmp_path / "local.db", now_ms=now)


# --- sessions: limits given to spendgate.local.session reach the ledger -----------------------


def test_session_step_limit_stops_the_third_call(tmp_path: Path) -> None:
    fake = FakeUpstream()
    with sync_client(tmp_path, fake) as client, session("nightly", max_steps=2):
        codes = [client.post(CHAT, json=BODY) for _ in range(3)]
    assert [r.status_code for r in codes] == [200, 200, 402]
    assert error_code(codes[2]) == "session_limit_reached"
    assert fake.calls == 2


def test_session_budget_is_its_own_cap(tmp_path: Path) -> None:
    fake = FakeUpstream()
    with sync_client(tmp_path, fake) as client:
        with session("tiny", budget="0.000001 USD"):
            refused = client.post(CHAT, json=BODY)
        outside = client.post(CHAT, json=BODY)  # the pool still has room
    assert (refused.status_code, error_code(refused)) == (402, "session_limit_reached")
    assert outside.status_code == 200


def test_session_limits_are_those_the_session_started_with(tmp_path: Path) -> None:
    with sync_client(tmp_path, FakeUpstream()) as client:
        with session("s", max_steps=1):
            first = client.post(CHAT, json=BODY)
        with session("s", max_steps=100):  # a later block cannot loosen a running session
            second = client.post(CHAT, json=BODY)
    assert (first.status_code, second.status_code) == (200, 402)


def test_a_bad_session_limit_raises_at_the_call_site(tmp_path: Path) -> None:
    with (
        sync_client(tmp_path, FakeUpstream()) as client,
        session("x", budget="2 EUR"),
        pytest.raises(ValueError, match="ledger currency"),
    ):
        client.post(CHAT, json=BODY)


# --- streams: the transport relays whatever the pipeline gives as `Reply.stream` ---------------


class _Pieces:
    """A `ByteStream` that yields two SSE pieces and records that it was closed."""

    def __init__(self) -> None:
        self._left = [b"data: one\n\n", b"data: two\n\n"]
        self.closed = False

    def __aiter__(self) -> "_Pieces":
        return self

    async def __anext__(self) -> bytes:
        if not self._left:
            raise StopAsyncIteration
        return self._left.pop(0)

    async def aclose(self) -> None:
        self.closed = True


def _stream_reply() -> Reply:
    headers = {"content-type": "text/event-stream", "content-length": "99"}
    return Reply(200, headers, b"", _Pieces())


@pytest.fixture
def streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handle(self: Runtime, call: Call) -> Reply:
        return _stream_reply()

    monkeypatch.setattr(Runtime, "handle", handle)


@pytest.mark.usefixtures("streaming")
def test_sync_transport_relays_a_stream_piece_by_piece(tmp_path: Path) -> None:
    with (
        sync_client(tmp_path, FakeUpstream()) as client,
        client.stream("POST", CHAT, json=BODY) as reply,
    ):
        pieces = list(reply.iter_raw())
    assert pieces == [b"data: one\n\n", b"data: two\n\n"]


@pytest.mark.usefixtures("streaming")
def test_async_transport_relays_a_stream(tmp_path: Path) -> None:
    async def main() -> list[bytes]:
        t = transport(
            settings_in(tmp_path), ledger_path=tmp_path / "local.db", cap="10 USD", async_=True,
            client=fake_client(FakeUpstream()), now_ms=now,
        )  # fmt: skip
        async with (
            httpx.AsyncClient(transport=t, base_url=BASE) as client,
            client.stream("POST", CHAT, json=BODY) as reply,
        ):
            return [piece async for piece in reply.aiter_raw()]

    assert asyncio.run(main()) == [b"data: one\n\n", b"data: two\n\n"]


def test_a_real_stream_is_relayed_and_settled(tmp_path: Path) -> None:
    fake = FakeUpstream()
    with sync_client(tmp_path, fake) as client:
        with client.stream("POST", CHAT, json={**BODY, "stream": True}) as reply:
            text = b"".join(reply.iter_raw()).decode()
        after = client.post(CHAT, json=BODY)  # the stream's charge is booked by now
    assert "Hel" in text and "[DONE]" in text
    left = Money.from_amount(after.headers["x-spendgate-budget-remaining"].split()[0], "USD")
    ten = Money.from_amount("10", "USD")
    assert ten.nano - left.nano > 2 * Money.from_amount("0.000003", "USD").nano  # two calls paid


# --- a bug caught by a catch-all leaves a redacted trace on `spendgate.local` ------------------


def test_a_catch_all_in_library_mode_logs_class_and_frames_but_not_the_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "MESSAGE-TEXT-MARKER"

    class Broken(OpenAIProvider):
        def response(self, req: CleanRequest, resp: UpstreamResponse) -> UpstreamResponse:
            raise KeyError(marker)

    def providers(settings: Settings) -> dict[str, Provider]:
        return build_providers(settings) | {"openai": Broken()}

    monkeypatch.setattr(runtime, "build_providers", providers)
    caplog.set_level(logging.ERROR, logger="spendgate.local")
    with sync_client(tmp_path, FakeUpstream()) as client:
        reply = client.post(CHAT, json=BODY)
    assert reply.status_code == 500 and error_code(reply) == "internal_error"
    (record,) = [r for r in caplog.records if r.name == "spendgate.local"]
    text = record.getMessage()
    assert "where=routing.response" in text and "error_class=KeyError" in text
    assert "charged=True" in text and "Forwarder._answered" in text
    assert reply.headers["x-spendgate-request-id"] in text
    assert marker not in caplog.text and record.exc_info is None
