"""One key's provider errors do not take a shared deployment away from other keys (ADR-0033).

A deployment's circuit breaker is shared by every key routed to it. A provider 403 or 404 used to
count as a deployment fault, and a caller could cause one on purpose (a chat alias on
/v1/embeddings draws an unbilled 403 from OpenAI; `test_endpoint_mode` now refuses that one before
upstream). A few of them opened the breaker and every other tenant got 503 for the cooldown. Now a
403 or 404 counts towards the breaker only when the provider's code says our key or deployment is
wrong; a 401 always counts, so a bad provider key still opens it.
"""

import json
from pathlib import Path

import pytest

from spendgate.core.outcomes import Outcome
from tests.fakes import Script
from tests.integration.harness import CHAT, Stack, async_test, open_stack
from tests.integration.uploads import second_key

MSG = [{"role": "user", "content": "hi"}]
BREAKER_TRIPS = 5  # `routing.min_requests`: the fifth failure in the window opens the breaker


def openai_error(status: int, code: str | None, message: str = "scripted") -> Script:
    error = {"message": message, "type": "invalid_request_error", "param": None, "code": code}
    return Script(status=status, body=json.dumps({"error": error}).encode())


async def chat_as(s: Stack, key: str) -> int:
    body = {"model": "alpha-one", "messages": MSG, "max_tokens": 16}
    return (await s.post(CHAT, body, key=key)).status_code


@async_test
async def test_t_dos_05_403_answers_caused_by_one_key_do_not_refuse_another_keys_calls(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path) as s:
        victim = await second_key(s)
        for _ in range(BREAKER_TRIPS * 2):  # far past the breaker's threshold
            s.fake.enqueue(openai_error(403, None, "You are not allowed to use this model."))
            assert await chat_as(s, s.secret) == 502  # upstream_unavailable: our config, unseen
        calls = s.fake.calls
        for _ in range(3):
            assert await chat_as(s, victim) == 200  # still served: the breaker stayed closed
        assert s.fake.calls == calls + 3
        outcomes = [a.outcome for a in s.observer.attempts]
        assert outcomes == [Outcome.CLIENT_ERROR] * BREAKER_TRIPS * 2 + [Outcome.OK] * 3
        assert await s.totals() == (3 * 3500, 0)  # the 403s were charged nothing


@async_test
async def test_t_dos_05_404_answers_without_a_provider_code_do_not_open_the_breaker(
    tmp_path: Path,
) -> None:
    """OpenAI's 404 for a model that is not served on this endpoint carries `code: null`."""
    async with open_stack(tmp_path) as s:
        for _ in range(BREAKER_TRIPS * 2):
            s.fake.enqueue(openai_error(404, None, "This model is only supported in v1/responses."))
            assert await chat_as(s, s.secret) == 502
        assert await chat_as(s, await second_key(s)) == 200


@pytest.mark.parametrize(
    "script",
    [
        openai_error(401, "invalid_api_key", "Incorrect API key provided"),
        openai_error(401, None, "You didn't provide an API key."),  # a 401 counts whatever its code
        openai_error(404, "model_not_found", "The model does not exist."),
        openai_error(403, "unsupported_country_region_territory", "Country not supported"),
    ],
    ids=["invalid_api_key", "401_without_code", "model_not_found", "unsupported_region"],
)
@async_test
async def test_an_invalid_provider_key_still_opens_the_breaker(
    tmp_path: Path, script: Script
) -> None:
    async with open_stack(tmp_path) as s:
        victim = await second_key(s)
        for _ in range(BREAKER_TRIPS):
            s.fake.enqueue(script)
            assert await chat_as(s, s.secret) == 502
        calls = s.fake.calls
        refused = await s.post(
            CHAT, {"model": "alpha-one", "messages": MSG, "max_tokens": 16}, key=victim
        )
        assert refused.status_code == 503  # open for every key: the deployment itself is broken
        assert refused.json()["error"]["code"] == "upstream_unavailable"
        assert s.fake.calls == calls  # refused without contacting the provider
        s.clock.advance(seconds=6)  # past `cooldown_seconds`: one probe, which succeeds
        assert await chat_as(s, victim) == 200


@async_test
async def test_a_bad_key_on_one_deployment_moves_traffic_to_the_next(tmp_path: Path) -> None:
    """With two deployments, the one whose key is refused is skipped once its breaker opens."""
    async with open_stack(tmp_path) as s:
        for _ in range(BREAKER_TRIPS):
            s.fake.enqueue(openai_error(401, "invalid_api_key"))
            assert (await s.chat("alpha")).status_code == 502  # a 401 is answered: no failover
        before = len(s.fake.requests)
        assert (await s.chat("alpha")).status_code == 200
        assert s.fake.requests[before].headers["host"] == "b.upstream.test"
