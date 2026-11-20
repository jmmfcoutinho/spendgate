"""Every refusal path makes zero upstream requests, in the real containers.

The upstream is the fake provider container (compose.test.yaml); its own request log is the
witness, read from outside the guard. Each case first proves the path is reachable (the right
refusal code), then that the log did not move. One allowed call shows the log can move at all.
"""

import json

import pytest

from tests.perimeter.conftest import APP_SCOPE, MAX_BODY_BYTES, TIGHT_SCOPE, Stack

pytestmark = pytest.mark.docker

CHAT = "/v1/chat/completions"
Alpha = {"model": "gpt-6-luna", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}


def test_one_allowed_call_reaches_the_fake(sqlite_stack: Stack) -> None:
    c = sqlite_stack.compose
    key = c.create_key(APP_SCOPE, "allowed")
    before = c.upstream_calls()
    reply = c.request(CHAT, key=key, body=Alpha)
    assert (reply.status, reply.code) == (200, None)
    assert c.upstream_calls() == before + 1


def test_refusals_reach_no_upstream(sqlite_stack: Stack) -> None:
    c = sqlite_stack.compose
    key = c.create_key(APP_SCOPE, "refused")
    tight = c.create_key(TIGHT_SCOPE, "tight")
    slow = c.create_key(APP_SCOPE, "slow", "--rpm", "1")
    # A virtual key shaped like a real one, but unknown to the ledger.
    forged = "sg-" + "0" * 12 + "-" + "0" * 48
    huge = {**Alpha, "messages": [{"role": "user", "content": "a" * (MAX_BODY_BYTES * 2)}]}
    embedding_as_chat = {**Alpha, "model": "embeddings"}  # priced for input only: unpriceable here
    c.request(CHAT, key=slow, body=Alpha)  # uses slow's one request this minute; reaches the fake
    before = c.upstream_calls()

    cases = {
        "T-BLK-01 unpriced": (
            c.request(CHAT, key=key, body=embedding_as_chat),
            404,
            "model_not_priced",
        ),
        "T-BLK-02 budget": (c.request(CHAT, key=tight, body=Alpha), 402, "budget_period_exhausted"),
        "T-BLK-04 bad key": (c.request(CHAT, key=forged, body=Alpha), 401, "unauthorized"),
        "no key": (c.request(CHAT, body=Alpha), 401, "unauthorized"),
        "T-BLK-05 too large": (c.request(CHAT, key=key, body=huge), 413, "request_too_large"),
        "rate limited": (c.request(CHAT, key=slow, body=Alpha), 429, "rate_limited"),
    }
    for name, (reply, status, code) in cases.items():
        assert (reply.status, reply.code) == (status, code), name
    assert c.upstream_calls() == before, "a refused request reached the upstream"


def test_kill_switch_reaches_no_upstream(sqlite_stack: Stack) -> None:
    c = sqlite_stack.compose
    key = c.create_key(APP_SCOPE, "killed")
    switch = json.loads(c.cli("kill", "on", "--reason", "perimeter test", "--json"))["id"]
    try:
        before = c.upstream_calls()
        reply = c.request(CHAT, key=key, body=Alpha)
        assert (reply.status, reply.code) == (503, "kill_switch")
        assert c.upstream_calls() == before
    finally:
        c.cli("kill", "off", switch)
    # Lifting it restores service, so the refusal above was the switch and nothing else.
    assert c.request(CHAT, key=key, body=Alpha).status == 200


def test_ledger_down_reaches_no_upstream(postgres_stack: Stack) -> None:
    """T-BLK-06: with the ledger unreachable the guard refuses; it does not guess and forward."""
    c = postgres_stack.compose
    key = c.create_key(APP_SCOPE, "ledger")
    before = c.upstream_calls()
    assert c.request(CHAT, key=key, body=Alpha).status == 200
    assert c.upstream_calls() == before + 1
    c.run("stop", "db")  # the stack's teardown removes everything, so no restart is needed
    reply = c.request(CHAT, key=key, body=Alpha)
    assert (reply.status, reply.code) == (503, "ledger_unavailable")
    assert c.upstream_calls() == before + 1
