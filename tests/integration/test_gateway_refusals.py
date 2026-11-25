"""Every refusal path makes zero upstream requests (M-12, T-BLK-06).

Each case is one request through the full stack (Starlette, pipeline, SQLite ledger); the fake
upstream's request log must stay empty, the budget untouched and the error body well formed.
"""

import asyncio
import re
from collections.abc import Callable, Coroutine
from pathlib import Path

import httpx
import pytest

from spendgate.core.outcomes import ReservationState
from spendgate.core.policy import KillSwitch, KillTarget
from spendgate.core.sessions import SessionDefaults
from spendgate.ledger.port import HoldRequest, LedgerPort, LedgerUnavailable
from tests.fakes import Script
from tests.integration.harness import NOW_MS, Stack, open_stack, usd, wait_until

HOLD_FORMAT = re.compile(r"\d+\.\d{9} USD")  # the `details.remaining` money format


def run(case: Callable[[Path], Coroutine[object, object, None]], tmp_path: Path) -> None:
    asyncio.run(case(tmp_path))


def error_of(response: httpx.Response) -> dict[str, object]:
    body = response.json()
    assert isinstance(body, dict) and isinstance(body["error"], dict)
    return body["error"]


async def refused(s: Stack, response: httpx.Response, status: int, code: str) -> None:
    """The shared assertions: status, stable code, request id, no upstream, nothing spent."""
    assert response.status_code == status, response.text
    assert error_of(response)["code"] == code
    assert response.headers["x-spendgate-request-id"].startswith("sg_req_")
    s.assert_no_contact()
    assert await s.totals() == (0, 0)


def test_bad_key_is_unauthorized(tmp_path: Path) -> None:
    async def case(path: Path) -> None:
        async with open_stack(path) as s:
            wrong = "sg-" + "0" * 12 + "-" + "0" * 48
            await refused(s, await s.post("/v1/chat/completions", {"model": "alpha"}, key=wrong),
                          401, "unauthorized")  # fmt: skip
            missing = await s.client.post("/v1/chat/completions", json={"model": "alpha"})
            await refused(s, missing, 401, "unauthorized")

    run(case, tmp_path)


# T-AUTH-02
def test_unknown_and_disallowed_models(tmp_path: Path) -> None:
    async def case(path: Path) -> None:
        async with open_stack(path, allowed_models=("alpha-one",)) as s:
            await refused(s, await s.chat("alpha"), 403, "model_not_allowed")
            await refused(s, await s.chat("no-such-model"), 403, "model_not_allowed")

    run(case, tmp_path)


# T-CAT-02
def test_unpriced_model_is_refused(tmp_path: Path) -> None:
    async def case(path: Path) -> None:
        async with open_stack(path) as s:
            s.clock.set(NOW_MS - 10 * 86_400_000)  # before the catalog entry takes effect
            await refused(s, await s.chat(), 404, "model_not_priced")

    run(case, tmp_path)


def test_over_budget_is_refused_before_upstream(tmp_path: Path) -> None:
    async def case(path: Path) -> None:
        async with open_stack(path, budget="0.000001") as s:
            r = await s.chat(max_tokens=1000)
            await refused(s, r, 402, "budget_exhausted")
            assert r.headers["x-should-retry"] == "false"
            assert "x-spendgate-cost" in r.headers

    run(case, tmp_path)


# T-SESS-01
def test_session_limit_is_refused_before_upstream(tmp_path: Path) -> None:
    async def case(path: Path) -> None:
        defaults = SessionDefaults(max_steps=1)
        async with open_stack(path, session_defaults=defaults) as s:
            hdr = {"X-Spendgate-Session": "run-1"}
            body: dict[str, object] = {
                "model": "alpha",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            }
            first = await s.post("/v1/chat/completions", body, headers=hdr)
            assert first.status_code == 200, first.text
            contacted = s.fake.calls
            second = await s.post("/v1/chat/completions", body, headers=hdr)
            assert second.status_code == 402
            assert error_of(second)["code"] == "session_limit_reached"
            assert s.fake.calls == contacted  # the refused call never reached the fake

    run(case, tmp_path)


# T-SESS-06
def test_session_header_needs_a_session_key(tmp_path: Path) -> None:
    async def case(path: Path) -> None:
        async with open_stack(path) as s:
            r = await s.post("/v1/chat/completions", {"model": "alpha"},
                             headers={"X-Spendgate-Session": "x"})  # fmt: skip
            await refused(s, r, 400, "invalid_request")
            assert error_of(r)["param"] == "X-Spendgate-Session"

    run(case, tmp_path)


SESSION = "X-Spendgate-Session"
# Header lists a client may send to a `session_required` key. Every one either is refused before
# the hold, or runs inside the session named last (repeated fields: the last value is the one
# the pipeline reads), so none of them gets a call past the session limits.
REQUIRED_CASES = {
    "absent": ([], None),
    "empty": ([(SESSION, "")], None),
    "space": ([(SESSION, " ")], None),
    "tab": ([(SESSION, "\t")], None),
    "padded": ([(SESSION, " run-1 ")], None),
    "repeated, last empty": ([(SESSION, "run-1"), (SESSION, "")], None),
    "repeated, first empty": ([(SESSION, ""), (SESSION, "run-2")], "run-2"),
    "repeated, both set": ([(SESSION, "run-3"), (SESSION, "run-4")], "run-4"),
    "plain": ([(SESSION, "run-5")], "run-5"),
}


# T-SESS-08
@pytest.mark.parametrize("case_name", list(REQUIRED_CASES))
def test_m11_a_session_required_key_refuses_a_call_without_the_header_before_the_hold(
    tmp_path: Path, case_name: str
) -> None:
    headers, session = REQUIRED_CASES[case_name]
    body = {"model": "alpha", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}

    async def case(path: Path) -> None:
        defaults = SessionDefaults(max_steps=1, required=True)
        async with open_stack(path, session_defaults=defaults) as s:
            sent = [("Authorization", f"Bearer {s.secret}"), *headers]
            r = await s.client.post("/v1/chat/completions", json=body, headers=sent)
            if session is None:
                await refused(s, r, 400, "invalid_request")
                assert error_of(r)["param"] == SESSION
                assert s.observer.held == []  # refused before any hold
                return
            assert r.status_code == 200, r.text
            # The call was a step of that session: with max_steps 1 the next one is refused.
            again = await s.post("/v1/chat/completions", body, headers={SESSION: session})
            assert error_of(again)["code"] == "session_limit_reached"

    run(case, tmp_path)


def test_kill_switch_is_refused(tmp_path: Path) -> None:
    async def case(path: Path) -> None:
        async with open_stack(path) as s:
            switch = KillSwitch(KillTarget.GLOBAL)
            await s.ledger.engage_kill_switch(switch, actor="t", reason="test", now=NOW_MS)
            await refused(s, await s.chat(), 503, "kill_switch")

    run(case, tmp_path)


# T-DOS-01
def test_oversized_body_is_refused_from_content_length(tmp_path: Path) -> None:
    async def case(path: Path) -> None:
        async with open_stack(path, max_body=1000) as s:
            big: dict[str, object] = {
                "model": "alpha",
                "messages": [{"role": "user", "content": "x" * 2000}],
            }
            await refused(s, await s.post("/v1/chat/completions", big), 413, "request_too_large")

    run(case, tmp_path)


# T-DOS-03
def test_oversized_chunked_body_hits_the_read_cap(tmp_path: Path) -> None:
    """No Content-Length: the streaming read stops at the cap instead of buffering it all."""

    async def case(path: Path) -> None:
        async with open_stack(path, max_body=1000) as s:
            read = 0

            async def body() -> object:
                nonlocal read
                for _ in range(100):
                    read += 100
                    yield b"x" * 100

            r = await s.client.post(
                "/v1/chat/completions", content=body(),  # type: ignore[arg-type]
                headers={"Authorization": f"Bearer {s.secret}", "Content-Type": "application/json"},
            )  # fmt: skip
            await refused(s, r, 413, "request_too_large")
            assert read < 10_000  # stopped early, did not drain the upload

    run(case, tmp_path)


# T-LIM-01
def test_rate_limit_is_refused_before_upstream(tmp_path: Path) -> None:
    async def case(path: Path) -> None:
        async with open_stack(path, rpm=1) as s:
            assert (await s.chat()).status_code == 200
            contacted = s.fake.calls
            r = await s.chat()
            assert r.status_code == 429 and error_of(r)["code"] == "rate_limited"
            assert error_of(r)["details"] == {"limit": "rpm", "retry_after_seconds": 60}
            assert r.headers["retry-after"] == "60"
            assert s.fake.calls == contacted

    run(case, tmp_path)


def test_refusal_with_room_left_names_the_worst_case_hold(tmp_path: Path) -> None:
    """Issue #16: the hold, not the budget, is too big; the code stays `budget_exhausted`."""

    async def case(path: Path) -> None:
        async with open_stack(path, budget="0.000001") as s:
            r = await s.chat(max_tokens=1000)
            await refused(s, r, 402, "budget_exhausted")
            error = error_of(r)
            details = error["details"]
            assert isinstance(details, dict) and isinstance(error["message"], str)
            assert details["remaining"] == "0.000001000 USD"
            hold = details["hold"]
            assert isinstance(hold, str) and usd(hold.split()[0]) > usd("0.000001")
            assert HOLD_FORMAT.fullmatch(hold)
            assert error["message"] == (
                f"The worst-case hold for this call ({hold}) exceeds the remaining budget for "
                f"{details['scope']} (0.000001000 USD). Set or lower max_tokens (or "
                "max_output_tokens) to lower the hold, or raise the cap."
            )

    run(case, tmp_path)


def test_refusal_with_nothing_left_says_the_budget_is_exhausted(tmp_path: Path) -> None:
    async def case(path: Path) -> None:
        async with open_stack(path, budget="0") as s:
            r = await s.chat(max_tokens=1000)
            await refused(s, r, 402, "budget_exhausted")
            error = error_of(r)
            details = error["details"]
            assert isinstance(details, dict) and details["remaining"] == "0.000000000 USD"
            assert error["message"] == f"Budget for {details['scope']} is exhausted."
            assert isinstance(details["hold"], str) and HOLD_FORMAT.fullmatch(details["hold"])

    run(case, tmp_path)


def test_refused_request_refunds_its_limits(tmp_path: Path) -> None:
    """Limits admitted at step 6 come back when the hold refuses (budget too small)."""

    async def case(path: Path) -> None:
        async with open_stack(path, budget="0.000001", rpm=1) as s:
            first = await s.chat(max_tokens=1000)
            assert error_of(first)["code"] == "budget_exhausted"
            second = await s.chat(max_tokens=1000)  # would be rate_limited if rpm leaked
            assert error_of(second)["code"] == "budget_exhausted"
            s.assert_no_contact()

    run(case, tmp_path)


def test_ledger_down_is_refused_without_contact(tmp_path: Path) -> None:
    class Down:
        """A ledger whose hold fails like a dead database; the rest passes through."""

        def __init__(self, inner: LedgerPort) -> None:
            self._inner = inner

        async def hold(self, request: HoldRequest) -> object:
            raise LedgerUnavailable("database is gone")

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    async def case(path: Path) -> None:
        async with open_stack(path, wrap_ledger=Down) as s:  # type: ignore[arg-type]
            r = await s.chat()
            assert r.status_code == 503 and error_of(r)["code"] == "ledger_unavailable"
            assert r.headers["retry-after"] == "5"
            s.assert_no_contact()
            assert await s.totals() == (0, 0)

    run(case, tmp_path)


class Stalled:
    """A ledger whose hold waits until the test lets it commit: a stuck database recovering late."""

    def __init__(self, inner: LedgerPort) -> None:
        self._inner = inner
        self.request_ids: list[str] = []
        self.recover = asyncio.Event()

    async def hold(self, request: HoldRequest) -> object:
        self.request_ids.append(request.request_id)
        await self.recover.wait()
        return await self._inner.hold(request)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def test_m12_a_hold_past_the_deadline_is_ledger_unavailable_and_released_when_it_lands(
    tmp_path: Path,
) -> None:
    stalled: list[Stalled] = []

    def wrap(ledger: LedgerPort) -> LedgerPort:
        stalled.append(Stalled(ledger))
        return stalled[0]  # type: ignore[return-value]

    async def case(path: Path) -> None:
        quick: dict[str, object] = {"hold_seconds": 0.05}
        async with open_stack(path, wrap_ledger=wrap, max_concurrent=1, pipeline_kwargs=quick) as s:
            try:
                r = await asyncio.wait_for(s.chat(), 5)  # without the deadline: never answered
            finally:
                stalled[0].recover.set()  # the hold now commits, after the caller was refused
            assert r.status_code == 503 and error_of(r)["code"] == "ledger_unavailable"
            s.assert_no_contact()
            await s.pipeline.aclose()  # waits for the give-back
            reservation = await s.ledger.get_reservation(stalled[0].request_ids[0])
            assert reservation is not None and reservation.state is ReservationState.RELEASED
            assert await s.totals() == (0, 0)
            again = await s.chat()  # the concurrency slot came back exactly once
            assert again.status_code == 200, again.text

    run(case, tmp_path)


def test_invalid_requests_are_refused(tmp_path: Path) -> None:
    async def case(path: Path) -> None:
        async with open_stack(path) as s:
            unknown = await s.chat(surprise=1)
            await refused(s, unknown, 400, "invalid_request")
            assert error_of(unknown)["param"] == "surprise"
            no_stream = await s.post(
                "/v1/embeddings", {"model": "embed", "input": "x", "stream": True}
            )
            await refused(s, no_stream, 400, "invalid_request")
            await refused(s, await s.chat(n=2), 400, "invalid_request")
            not_json = await s.client.post(
                "/v1/chat/completions", content=b"{nope",
                headers={"Authorization": f"Bearer {s.secret}", "Content-Type": "application/json"},
            )  # fmt: skip
            await refused(s, not_json, 400, "invalid_request")
            wrong_type = await s.client.post(
                "/v1/chat/completions", content=b"x",
                headers={"Authorization": f"Bearer {s.secret}", "Content-Type": "text/plain"},
            )  # fmt: skip
            await refused(s, wrong_type, 415, "invalid_request")  # protocol 2.1

    run(case, tmp_path)


def test_per_call_ceiling_and_output_ceiling(tmp_path: Path) -> None:
    async def case(path: Path) -> None:
        async with open_stack(path, per_call=usd("0.0001")) as s:
            over = await s.chat(max_tokens=100_000)  # worst case far above 0.0001 USD
            await refused(s, over, 413, "request_too_large")
            too_big = await s.chat(max_tokens=10**7)  # above the catalog ceiling
            await refused(s, too_big, 413, "request_too_large")

    run(case, tmp_path)


def test_concurrency_gate_refuses_fast_when_full(tmp_path: Path) -> None:
    """The semaphore is taken before the body is read; a full gate never reads the upload."""

    async def case(path: Path) -> None:
        async with open_stack(path, concurrent=1) as s:
            s.fake.enqueue(Script(hang=True))
            first = asyncio.ensure_future(s.chat())
            await wait_until(lambda: s.fake.calls >= 1)
            contacted = s.fake.calls
            second = await s.chat()
            assert second.status_code == 429 and error_of(second)["code"] == "rate_limited"
            assert s.fake.calls == contacted
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            await s.pipeline.aclose()

    run(case, tmp_path)


def test_unknown_v1_path_is_not_found(tmp_path: Path) -> None:
    async def case(path: Path) -> None:
        async with open_stack(path) as s:
            r = await s.client.post("/v1/files", json={})
            assert r.status_code == 404 and error_of(r)["code"] == "not_found"
            anthropic = await s.client.post("/v1/messages/batches", json={})
            assert anthropic.json()["type"] == "error"
            s.assert_no_contact()

    run(case, tmp_path)
