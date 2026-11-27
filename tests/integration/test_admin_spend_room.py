"""`keys:write` alone cannot create spend room (ADR-0030, threat model C-04).

T-ADM-06: setting or raising a session budget, or lifting or raising the new-session bound, needs
`budgets:write` as well. T-ADM-07: rotating a key keeps its key kill switch. M-17 end to end: a
session budget on a chain with no other budget is no budget, and nothing reaches the provider.
"""

from collections.abc import Mapping
from pathlib import Path

from spendgate.ledger.key_types import AdminScope
from tests.integration.harness import async_test
from tests.integration.test_admin_support import Admin, open_admin

BASE = "/admin/v1"
PATH = "org:acme/user:u1"
KEYS, MONEY = AdminScope.KEYS_WRITE, AdminScope.BUDGETS_WRITE
BIG = 10**15  # 1,000,000 units of the ledger currency


async def chat_as(a: Admin, secret: str, session: str | None = None) -> int:
    body: dict[str, object] = {"model": "alpha", "max_tokens": 5}
    body["messages"] = [{"role": "user", "content": "hi"}]
    headers = {} if session is None else {"X-Spendgate-Session": session}
    return (
        await a.stack.post("/v1/chat/completions", body, key=secret, headers=headers)
    ).status_code


def key_body(**session: object) -> dict[str, object]:
    return {"scope_id": PATH, "session_defaults": session}


async def refused(a: Admin, method: str, url: str, key: str, body: Mapping[str, object]) -> str:
    err = await a.error(method, url, 403, key=key, json=body)
    assert (err["code"], err["message"]) == ("forbidden", "This key may not do that.")
    return str(err["param"])


@async_test
async def test_t_adm_06_keys_write_alone_cannot_set_or_raise_session_budgets(
    tmp_path: Path,
) -> None:
    async with open_admin(tmp_path) as a:
        keys = await a.admin_key(KEYS)
        url = f"{BASE}/keys"
        budget = "session_defaults.budget_nano"
        rate = "session_defaults.max_new_sessions_per_hour"
        assert await refused(a, "POST", url, keys, key_body(budget_nano=BIG)) == budget
        assert await refused(a, "POST", url, keys, key_body(max_new_sessions_per_hour=None)) == rate
        assert await refused(a, "POST", url, keys, key_body(max_new_sessions_per_hour=61)) == rate
        # Narrowing, and limits that cannot move money, need only keys:write.
        made = await a.ok("POST", url, 201, key=keys, json=key_body(max_steps=3))
        await a.ok("POST", url, 201, key=keys, json=key_body(max_new_sessions_per_hour=10))
        plain = await a.ok("POST", url, 201, key=keys, json={"scope_id": PATH})
        # PATCH compares with what is stored; a key without sessions counts as the defaults.
        for target, body in (
            (made, {"session_defaults": {"budget_nano": 5}}),
            (plain, {"session_defaults": {"budget_nano": 5}}),
            (made, {"session_defaults": {"max_new_sessions_per_hour": None}}),
            (plain, {"session_defaults": {"max_new_sessions_per_hour": 61}}),
        ):
            assert await refused(a, "PATCH", f"{url}/{target['id']}", keys, body)
        # With budgets:write as well, the same changes go through; lowering then needs neither.
        both = await a.admin_key(KEYS, MONEY)
        rich = await a.ok("POST", url, 201, key=both, json=key_body(budget_nano=BIG))
        rich_url = f"{url}/{rich['id']}"
        lower = {"session_defaults": {"budget_nano": BIG // 2, "max_new_sessions_per_hour": 5}}
        await a.ok("PATCH", rich_url, key=keys, json=lower)
        higher = {"session_defaults": {"budget_nano": BIG, "max_new_sessions_per_hour": 5}}
        assert await refused(a, "PATCH", rich_url, keys, higher) == budget
        await a.ok("PATCH", rich_url, key=both, json=higher)
        stored = await a.ok("GET", rich_url)
        assert stored["session_defaults"] == higher["session_defaults"]


@async_test
async def test_m17_keys_write_on_a_budgetless_org_gets_no_spend_room(tmp_path: Path) -> None:
    """The SG-PT-01 path: a new org with no budget, a key whose sessions carry a budget."""
    async with open_admin(tmp_path) as a:
        keys = await a.admin_key(KEYS)
        org = await a.ok("POST", f"{BASE}/scopes", 201, key=keys, json={"path": "org:evil"})
        body = {
            "scope_id": org["id"],
            "session_defaults": {"budget_nano": BIG, "max_new_sessions_per_hour": None},
        }
        made = await a.ok("POST", f"{BASE}/keys", 201, json=body)  # even an all-scope admin
        secret = str(made["secret"])
        for session in (None, "s1", "s2"):
            assert await chat_as(a, secret, session) == 403
        assert a.stack.fake.calls == 0
        sessions = await a.ok("GET", f"{BASE}/scopes", params={"kind": "session"})
        assert sessions["data"] == []


@async_test
async def test_t_adm_07_rotation_keeps_the_kill_switch(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        keys = await a.admin_key(KEYS)
        made = await a.ok("POST", f"{BASE}/keys", 201, json={"scope_id": PATH})
        assert await chat_as(a, str(made["secret"])) == 200
        kill = {"target_kind": "key", "target_ref": made["id"], "reason": "stop it"}
        switch = await a.ok("POST", f"{BASE}/kill-switches", 201, json=kill)
        rotate = f"{BASE}/keys/{made['id']}/rotate"
        fresh = await a.ok("POST", rotate, 201, key=keys, json={"grace_seconds": 600})
        again = await a.ok("POST", f"{BASE}/keys/{fresh['id']}/rotate", 201, key=keys)
        calls = a.stack.fake.calls
        for secret in (made["secret"], fresh["secret"], again["secret"]):
            assert await chat_as(a, str(secret)) == 503
        assert a.stack.fake.calls == calls
        await a.ok("DELETE", f"{BASE}/kill-switches/{switch['id']}", 204)
        for secret in (made["secret"], fresh["secret"], again["secret"]):
            assert await chat_as(a, str(secret)) == 200
