"""Library mode decides like the proxy: one table of requests, run through both, compared.

The gateway side is the full Starlette stack from `harness.py`; the local side is the httpx
transport over the *same* `Settings`. Each side has its own fake upstream with the same script, so
what is compared is the observable outcome: status, error code, charge and upstream contacts.
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from spendgate.local import transport
from tests.fakes import FakeUpstream, FakeUsage, Script
from tests.integration.harness import MESSAGES_PATH, open_stack
from tests.integration.test_local_support import BODY, CHAT, now

CHAT_BODY = {**BODY}
MESSAGES_BODY = {
    "model": "haiku",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "hi"}],
}


@dataclass(frozen=True, slots=True)
class Case:
    name: str
    path: str = CHAT
    body: dict[str, object] = field(default_factory=lambda: dict(CHAT_BODY))
    scripts: tuple[Script, ...] = ()
    budget: str = "10"


CASES = (
    Case("chat ok"),
    Case("anthropic messages ok", MESSAGES_PATH, MESSAGES_BODY),
    Case("embedding ok", "/v1/embeddings", {"model": "embed", "input": "hello"}),
    Case("stream ok", body={**CHAT_BODY, "stream": True}),
    Case("unknown model", body={**CHAT_BODY, "model": "nope"}),
    Case("over budget", budget="0.000001"),
    Case("no messages", body={"model": "alpha", "max_tokens": 100}),
    Case("failover after a 500", scripts=(Script(status=500),)),
    Case("both deployments throttle", scripts=(Script(status=429), Script(status=429))),
    Case("provider rejects the request", scripts=(Script(status=400),)),
    Case("big usage", scripts=(Script(usage=FakeUsage(input_tokens=50, output_tokens=100)),)),
)


def outcome(response: httpx.Response, fake: FakeUpstream) -> tuple[object, ...]:
    body = (
        response.json() if response.headers["content-type"].startswith("application/json") else {}
    )
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return (
        response.status_code, code, response.headers.get("x-spendgate-cost"),
        response.headers.get("x-spendgate-budget-remaining"), fake.calls,
    )  # fmt: skip


async def through_gateway(case: Case, tmp_path: Path) -> tuple[object, ...]:
    async with open_stack(tmp_path / "gw", budget=case.budget) as s:
        s.fake.enqueue(*case.scripts)
        return outcome(await s.post(case.path, case.body), s.fake)


async def through_local(case: Case, tmp_path: Path) -> tuple[object, ...]:
    async with open_stack(tmp_path / "ref", budget=case.budget) as s:  # only for its Settings
        settings = s.settings
    fake = FakeUpstream()
    fake.enqueue(*case.scripts)
    t = transport(
        settings, ledger_path=tmp_path / "local.db", cap=f"{case.budget} USD",
        async_=True, client=fake.client(), now_ms=now,
    )  # fmt: skip
    async with httpx.AsyncClient(transport=t, base_url="http://spendgate.local") as client:
        return outcome(await client.post(case.path, json=case.body), fake)


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_same_request_same_decision_and_charge(case: Case, tmp_path: Path) -> None:
    (tmp_path / "gw").mkdir()
    (tmp_path / "ref").mkdir()

    async def both() -> tuple[tuple[object, ...], tuple[object, ...]]:
        return await through_gateway(case, tmp_path), await through_local(case, tmp_path)

    proxy, local = asyncio.run(both())
    assert local == proxy
