"""Scripts that produce each settlement-table outcome, and a stand-in classifier to prove it.

`observe` is deliberately naive: it is not the pipeline's classifier, only enough logic to show
that the fake's behaviour leads to each `Outcome`. Pipeline and load tests reuse the scripts.
"""

import asyncio
from dataclasses import dataclass
from typing import Literal

import httpx

from spendgate.core.outcomes import SETTLEMENT, Outcome, outcome_for_status
from tests.fakes.script import Script
from tests.fakes.upstream import FakeUpstream

FREE_STATUSES = frozenset({429})
Mode = Literal["normal", "timeout", "cancel", "abandon", "gateway_fault"]
CHAT = "/v1/chat/completions"
STREAM_BODY = {"model": "m", "stream": True, "stream_options": {"include_usage": True}}


@dataclass(frozen=True, slots=True)
class Scenario:
    script: Script
    stream: bool = False
    mode: Mode = "normal"  # who ends the exchange: the network, a timer, the client, a gateway bug


OUTCOME_SCENARIOS: dict[Outcome, Scenario] = {
    Outcome.OK: Scenario(Script()),
    Outcome.OK_NO_USAGE: Scenario(Script(final_usage=False), stream=True),
    Outcome.CLIENT_ERROR: Scenario(Script(status=400)),
    Outcome.THROTTLED: Scenario(Script(status=429, retry_after="3")),
    Outcome.TIMEOUT: Scenario(Script(hang=True), mode="timeout"),
    Outcome.UPSTREAM_5XX: Scenario(Script(status=503)),
    Outcome.UNREACHABLE: Scenario(Script(refuse_connection=True)),
    Outcome.CANCELLED: Scenario(Script(hang=True), mode="cancel"),
    Outcome.STREAM_BROKEN: Scenario(Script(cut_after_frames=2), stream=True),
    # After contact the gateway itself fails (a bug): the fake only supplies the contact.
    Outcome.INTERNAL_ERROR: Scenario(Script(), mode="gateway_fault"),
    # Sent and never answered, and nobody settles it: the sweeper's case.
    Outcome.EXPIRED: Scenario(Script(hang=True), mode="abandon"),
}
assert set(OUTCOME_SCENARIOS) == set(SETTLEMENT)  # a new outcome needs a new scenario


async def observe(fake: FakeUpstream, scenario: Scenario) -> Outcome:
    """Run the scenario against `fake` and name the outcome it leads to."""
    fake.enqueue(scenario.script)
    async with fake.client() as client:
        if scenario.mode in ("timeout", "cancel", "abandon"):
            return await _interrupted(fake, client, scenario)
        outcome = await _exchange(client, scenario.stream)
        if scenario.mode == "gateway_fault":
            return Outcome.INTERNAL_ERROR if fake.calls else outcome
        return outcome


def _post(client: httpx.AsyncClient, stream: bool) -> asyncio.Future[httpx.Response]:
    body = STREAM_BODY if stream else {"model": "m"}
    return asyncio.ensure_future(client.post(CHAT, json=body))


async def _exchange(client: httpx.AsyncClient, stream: bool) -> Outcome:
    body = STREAM_BODY if stream else {"model": "m"}
    try:
        async with client.stream("POST", CHAT, json=body) as response:
            if response.status_code >= 400:
                await response.aread()
                return outcome_for_status(response.status_code, FREE_STATUSES)
            text = (await response.aread()).decode()
    except httpx.ConnectError:
        return Outcome.UNREACHABLE
    except httpx.RemoteProtocolError:
        return Outcome.STREAM_BROKEN
    return Outcome.OK if '"usage"' in text else Outcome.OK_NO_USAGE


async def _interrupted(
    fake: FakeUpstream, client: httpx.AsyncClient, scenario: Scenario
) -> Outcome:
    pending = _post(client, scenario.stream)
    await fake.wait_for_calls(1)  # the request was sent; now the exchange is cut short
    if scenario.mode == "abandon":
        still_open = not pending.done()
        pending.cancel()  # test cleanup only; the sweeper would be the one settling it
        return Outcome.EXPIRED if still_open else Outcome.OK
    if scenario.mode == "cancel":
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            return Outcome.CANCELLED
        return Outcome.OK
    try:
        async with asyncio.timeout(None) as deadline:
            deadline.reschedule(asyncio.get_running_loop().time())
            await pending
    except TimeoutError:
        pending.cancel()
        return Outcome.TIMEOUT
    return Outcome.OK
