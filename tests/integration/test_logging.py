"""Key material, prompts and answers stay out of the ledger and the error bodies (C-17, TH-08).

T-LOG-01 (log lines) is in `tests/unit/test_observability.py`, T-LOG-04 (bootstrap output) in
`tests/integration/test_admin_bootstrap.py`. Here: what the ledger file holds after real calls
(T-LOG-02) and what a client is told when a call is refused or the provider fails (T-LOG-03).
"""

from pathlib import Path

import httpx
import pytest

from tests.fakes import Script
from tests.integration.harness import CHAT, ENV, PEPPER, Stack, async_test, open_stack

PROMPT = "fake-prompt-marker-4711"
ANSWER = ("fake-answer-", "marker-4711")
PROVIDER_KEY = ENV["OPENAI_KEY"]


def forged(secret: str) -> str:
    """The same prefix with another secret: well formed, found, and wrong."""
    tag, prefix, rest = secret.split("-")
    return f"{tag}-{prefix}-{'0' * len(rest)}"


def secret_part(key: str) -> str:
    return key.rsplit("-", 1)[1]


def ledger_bytes(tmp_path: Path) -> bytes:
    """Every file of the SQLite ledger (main file, WAL, shared memory) as raw bytes."""
    files = sorted(tmp_path.glob("ledger.db*"))
    assert files, "no ledger file"
    return b"".join(f.read_bytes() for f in files)


async def chat(s: Stack, *, key: str | None = None, **extra: object) -> httpx.Response:
    body: dict[str, object] = {"model": "alpha", "max_tokens": 100, "user": "u-1"}
    body["messages"] = [{"role": "user", "content": PROMPT}]
    return await s.post(CHAT, body | extra, key=key)


@async_test
async def test_t_log_02_the_ledger_holds_no_prompt_answer_or_key_material(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        presented = [s.secret, forged(s.secret)]
        assert (await chat(s)).status_code == 200
        s.fake.enqueue(Script(deltas=ANSWER))
        streamed = await chat(s, stream=True)
        assert streamed.status_code == 200 and "marker-4711" in streamed.text  # it was relayed
        assert (await chat(s, key=presented[1])).status_code == 401
        s.fake.enqueue(Script(status=500))
        assert (await chat(s)).status_code >= 500
        assert (await chat(s, model="not-a-model")).status_code >= 400
        spent, _ = await s.totals()
        assert spent > 0  # calls were booked, so the ledger was written
    stored = ledger_bytes(tmp_path)
    for marker in (PROMPT, "".join(ANSWER), PROVIDER_KEY, PEPPER.decode()):
        assert marker.encode() not in stored, marker
    for key in presented:
        assert key.encode() not in stored
        assert secret_part(key).encode() not in stored


async def leaky_upstream(request: httpx.Request) -> httpx.Response:
    """A provider that echoes the credentials it was sent into its error message and headers."""
    echoed = f"bad key {request.headers.get('authorization')} {PROVIDER_KEY}"
    error = {"error": {"message": echoed, "type": "invalid_request_error", "code": "bad"}}
    return httpx.Response(400, json=error, headers={"x-echo": echoed})


@pytest.mark.parametrize("stream", [False, True])
@async_test
async def test_t_log_03_a_provider_error_that_echoes_keys_reaches_the_client_without_them(
    tmp_path: Path, stream: bool
) -> None:
    sender = httpx.AsyncClient(transport=httpx.MockTransport(leaky_upstream))
    async with open_stack(tmp_path, pipeline_kwargs={"sender": sender}) as s:
        r = await chat(s, stream=stream)
        assert r.status_code == 400, r.text
        seen = r.text + "".join(f"{k}: {v}\n" for k, v in r.headers.items())
        for secret in (PROVIDER_KEY, s.secret, secret_part(s.secret)):
            assert secret not in seen
    await sender.aclose()


@async_test
async def test_t_log_03_refusals_never_carry_the_presented_key(tmp_path: Path) -> None:
    async with open_stack(tmp_path, budget="0", allowed_models=("alpha",)) as s:
        wrong = forged(s.secret)
        mismatch = {"Authorization": f"Bearer {s.secret}", "x-api-key": wrong}
        replies = [
            await chat(s, key=wrong),  # 401: found prefix, wrong secret
            await s.client.post(CHAT, json={"model": "alpha"}, headers=mismatch),  # 401
            await s.client.post(CHAT, json={"model": "alpha"}, headers={"x-api-key": wrong}),
            await chat(s),  # 402: the budget is 0
            await chat(s, model="haiku"),  # 403: not an allowed model
            await chat(s, model="not-a-model"),  # 403: unknown, so not allowed either
            await s.post(CHAT, {"model": "alpha"}, headers={"x-spendgate-user": "bad user"}),
        ]
        assert [r.status_code for r in replies] == [401, 401, 401, 402, 403, 403, 400]
        s.assert_no_contact()
        for r in replies:
            seen = r.text + "".join(f"{k}: {v}\n" for k, v in r.headers.items())
            for key in (s.secret, wrong):
                assert key not in seen and secret_part(key) not in seen
