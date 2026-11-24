"""A slow credential refresh never stalls the gateway: it runs in a worker thread (issue #20).

The Bedrock adapter stands in for every token source (Entra ID, google-auth, the AWS chain):
its credentials block on a `Gate` until the test opens it, while other requests go through.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path

from spendgate.providers.bedrock import AwsCredentials, Bedrock
from tests.fakes.gate import GATE_TIMEOUT_S, Gate
from tests.integration.harness import AWS_EXAMPLE, async_test, open_stack


def gated(gate: Gate) -> Callable[[], AwsCredentials]:
    def get() -> AwsCredentials:
        gate.refresh()
        return AWS_EXAMPLE

    return get


@async_test
async def test_a_slow_credential_refresh_does_not_stall_other_requests(tmp_path: Path) -> None:
    gate = Gate()
    providers = {"bedrock": Bedrock(gated(gate))}
    async with open_stack(tmp_path, providers=providers) as s:
        slow = asyncio.create_task(s.chat("bedrock-haiku"))
        assert await asyncio.to_thread(gate.entered.wait, GATE_TIMEOUT_S)
        other = await s.chat("alpha")  # served while the refresh is still blocked
        assert other.status_code == 200, other.text
        assert not slow.done() and not gate.opened.is_set()
        gate.opened.set()
        answer = await slow
        assert answer.status_code == 200, answer.text
        assert gate.calls == 1


@async_test
async def test_concurrent_requests_share_one_credential_refresh(tmp_path: Path) -> None:
    gate = Gate()
    providers = {"bedrock": Bedrock(gated(gate))}
    async with open_stack(tmp_path, providers=providers) as s:
        calls = asyncio.gather(*(s.chat("bedrock-haiku") for _ in range(5)))
        answers, _ = await asyncio.gather(calls, _open_when_entered(gate))
        assert [r.status_code for r in answers] == [200] * 5
        assert gate.calls == 1


async def _open_when_entered(gate: Gate) -> None:
    assert await asyncio.to_thread(gate.entered.wait, GATE_TIMEOUT_S)
    gate.opened.set()


@async_test
async def test_a_failed_refresh_is_an_internal_error_and_nothing_is_sent(tmp_path: Path) -> None:
    gate = Gate(fail=True)
    gate.opened.set()
    providers = {"bedrock": Bedrock(gated(gate))}
    async with open_stack(tmp_path, providers=providers) as s:
        r = await s.chat("bedrock-haiku")
        assert r.status_code == 500
        assert r.json()["error"]["code"] == "internal_error"
        s.assert_no_contact()  # fail closed: never sent unsigned
        assert await s.totals() == (0, 0)  # the hold is released: nothing was sent
