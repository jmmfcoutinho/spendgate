"""Admin API keys for agents: `session_required` and the default new-session bound (TH-09)."""

from pathlib import Path

from tests.integration.harness import async_test
from tests.integration.test_admin_support import Admin, open_admin

BASE = "/admin/v1"
PATH = "org:acme/user:u1"
CHAT = "/v1/chat/completions"
BODY: dict[str, object] = {
    "model": "alpha", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}],
}  # fmt: skip


async def chat_as(a: Admin, secret: str, session: str | None = None) -> int:
    headers = {} if session is None else {"X-Spendgate-Session": session}
    return (await a.stack.post(CHAT, BODY, key=secret, headers=headers)).status_code


@async_test
async def test_m11_a_session_required_key_made_by_the_api_refuses_calls_without_the_header(
    tmp_path: Path,
) -> None:
    async with open_admin(tmp_path) as a:
        made = await a.ok(
            "POST", f"{BASE}/keys", 201,
            json={"scope_id": PATH, "session_required": True,
                  "session_defaults": {"max_steps": 5}},
        )  # fmt: skip
        assert made["session_required"] is True
        # The new-session bound is on unless the request sends null.
        assert made["session_defaults"] == {"max_steps": 5, "max_new_sessions_per_hour": 60}
        secret = str(made["secret"])
        assert await chat_as(a, secret) == 400
        assert await chat_as(a, secret, "run-1") == 200
        listed = await a.ok("GET", f"{BASE}/keys/{made['id']}")
        assert listed["session_required"] is True


@async_test
async def test_session_required_needs_session_defaults_on_create_and_update(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        alone = {"scope_id": PATH, "session_required": True}
        bad = await a.error("POST", f"{BASE}/keys", 400, json=alone)
        assert (bad["code"], bad["param"]) == ("invalid_request", "session_required")
        plain = await a.ok("POST", f"{BASE}/keys", 201, json={"scope_id": PATH})
        assert plain["session_required"] is False and "session_defaults" not in plain
        url = f"{BASE}/keys/{plain['id']}"
        bad = await a.error("PATCH", url, 400, json={"session_required": True})
        assert bad["param"] == "session_required"


@async_test
async def test_key_update_keeps_session_required_unless_it_is_sent(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        made = await a.ok(
            "POST", f"{BASE}/keys", 201,
            json={"scope_id": PATH, "session_defaults": {"max_new_sessions_per_hour": None}},
        )  # fmt: skip
        assert made["session_defaults"] == {}  # null opts out of the new-session bound
        url = f"{BASE}/keys/{made['id']}"
        on = await a.ok("PATCH", url, json={"session_required": True})
        assert on["session_required"] is True and on["session_defaults"] == {}
        limits = await a.ok("PATCH", url, json={"session_defaults": {"max_steps": 3}})
        assert limits["session_required"] is True  # new limits keep the flag
        assert limits["session_defaults"] == {"max_steps": 3, "max_new_sessions_per_hour": 60}
        await a.error("PATCH", url, 400, json={"session_defaults": None})  # flag still on
        off = await a.ok("PATCH", url, json={"session_required": False, "session_defaults": None})
        assert off["session_required"] is False and "session_defaults" not in off
        assert await chat_as(a, str(made["secret"])) == 200
