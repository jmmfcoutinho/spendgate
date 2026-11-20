"""Regression tests for two defects the load run found (both fixed): every `serve` process
named itself 'gateway', and a client leaving before its body was read leaked a traceback.
"""

from pathlib import Path

from starlette.types import Message

from spendgate.gateway.wiring import build_pipeline
from tests.integration.harness import CHAT, async_test, open_stack


@async_test
async def test_each_gateway_instance_has_its_own_lease_owner_name(
    tmp_path: Path,
) -> None:
    """`serve` builds its pipeline through `build_pipeline`, which never passes `instance`, so all
    processes write owner_instance = 'gateway'. `renew_all('gateway')` of any live peer then keeps
    a crashed peer's holds alive for ever and the sweeper never charges them (ledger-state-machine:
    a restarted instance gets a new owner_instance). Seen in the load run: 117 holds still pending
    25 s after kill -9 with a 4 s lease."""
    async with open_stack(tmp_path) as stack:
        sender = stack.fake.client()
        args = (stack.settings, stack.ledger, stack.keys, sender)
        first, second = build_pipeline(*args), build_pipeline(*args)
        try:
            assert first._instance != second._instance
        finally:
            await sender.aclose()


@async_test
async def test_client_leaving_before_the_body_is_read_is_not_an_unhandled_error(
    tmp_path: Path,
) -> None:
    """`read_capped` lets starlette's ClientDisconnect escape, so uvicorn logs 'Exception in ASGI
    application' and a traceback for an ordinary cancelled request (before auth and any hold: no
    money effect). Seen in the load run with client-side cancels."""
    async with open_stack(tmp_path) as stack:

        async def receive() -> Message:
            return {"type": "http.disconnect"}

        async def send(_message: Message) -> None:
            return None

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
            "path": CHAT, "raw_path": CHAT.encode(), "query_string": b"", "root_path": "",
            "scheme": "http", "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80),
            "headers": [
                (b"authorization", f"Bearer {stack.secret}".encode()),
                (b"content-type", b"application/json"),
                (b"content-length", b"100"),
            ],
        }  # fmt: skip
        await stack.app(scope, receive, send)
