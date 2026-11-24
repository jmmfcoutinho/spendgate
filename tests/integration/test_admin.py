"""The admin API route by route: auth and scopes, validation, money as strings, audit rows."""

from pathlib import Path
from urllib.parse import quote

from spendgate.ledger.key_types import AdminScope
from tests.fakes import ABSURD_USAGE, FakeUpstream, Script
from tests.integration.harness import NOW_MS, async_test
from tests.integration.test_admin_support import Admin, open_admin

BASE = "/admin/v1"
PATH = "org:acme/user:u1"


def enc(path: str) -> str:
    return quote(path, safe="")


async def chat_as(a: Admin, secret: str) -> int:
    body: dict[str, object] = {"model": "alpha", "max_tokens": 5}
    body["messages"] = [{"role": "user", "content": "hi"}]
    return (await a.stack.post("/v1/chat/completions", body, key=secret)).status_code


# --- auth and scopes (T-ADM-01, T-ADM-02) ------------------------------------------------------


@async_test
async def test_no_key_and_wrong_kind_of_key_are_unauthorized(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        for key in ("", "sg-123456789abc-" + "0" * 48, "garbage", a.stack.secret):
            err = await a.error("GET", f"{BASE}/status", 401, key=key)
            assert err["code"] == "unauthorized"


@async_test
async def test_a_virtual_key_is_refused_on_admin_and_an_admin_key_on_the_data_plane(
    tmp_path: Path,
) -> None:
    async with open_admin(tmp_path) as a:
        r = await a.stack.client.post(
            "/v1/chat/completions",
            json={"model": "alpha", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {a.secret}"},
        )
        assert r.status_code == 401
        assert a.stack.fake.calls == 0


@async_test
async def test_each_admin_scope_allows_only_its_routes(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        read = await a.admin_key(AdminScope.USAGE_READ)
        keys = await a.admin_key(AdminScope.KEYS_WRITE)
        money = await a.admin_key(AdminScope.BUDGETS_WRITE)
        scope_body = {"json": {"path": "org:other"}}
        await a.ok("GET", f"{BASE}/status", key=read)
        for key in (keys, money):
            assert (await a.error("GET", f"{BASE}/status", 403, key=key))["code"] == "forbidden"
        for key in (read, money):
            err = await a.error("POST", f"{BASE}/scopes", 403, key=key, **scope_body)
            assert err["code"] == "forbidden"
        await a.ok("POST", f"{BASE}/scopes", 201, key=keys, **scope_body)
        budget = {"json": {"cap": {"amount": "5", "currency": "USD"}}}
        for key in (read, keys):
            err = await a.error(
                "POST", f"{BASE}/scopes/{enc(PATH)}/budgets", 403, key=key, **budget
            )
            assert err["code"] == "forbidden"
        await a.ok("POST", f"{BASE}/scopes/{enc(PATH)}/budgets", 201, key=money, **budget)
        kill = {"json": {"target_kind": "global", "reason": "x"}}
        assert (await a.error("POST", f"{BASE}/kill-switches", 403, key=read, **kill))["code"]
        assert (await a.error("GET", f"{BASE}/keys", 403, key=money))["code"] == "forbidden"


@async_test
async def test_a_revoked_admin_key_stops_working(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        await a.stack.ledger.revoke_key(a.key_id, actor="t", now=NOW_MS)
        a.stack.keys.invalidate(a.key_id)
        await a.error("GET", f"{BASE}/status", 401)


@async_test
async def test_unknown_admin_path_and_bad_json_are_json_errors(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        assert (await a.error("GET", f"{BASE}/nothing", 404))["code"] == "not_found"
        r = await a.call("POST", f"{BASE}/scopes", content=b"{not json")
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
        err = await a.error("POST", f"{BASE}/scopes", 400, json={"path": "org:x", "extra": 1})
        assert err["code"] == "invalid_request"  # unknown fields are refused


# --- scopes ----------------------------------------------------------------------------------


@async_test
async def test_scopes_create_list_get_and_update(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        made = await a.ok(
            "POST", f"{BASE}/scopes", 201,
            json={"path": "org:acme/project:app", "display_name": "The app"},
        )  # fmt: skip
        assert made["kind"] == "project" and made["display_name"] == "The app"
        assert made["path"] == "org:acme/project:app"
        dup = await a.error("POST", f"{BASE}/scopes", 409, json={"path": "org:acme/project:app"})
        assert dup["code"] == "conflict"
        orphan = await a.error("POST", f"{BASE}/scopes", 404, json={"path": "org:none/project:x"})
        assert orphan["param"] == "path"
        for bad in ("org:acme/session:x", "org:acme/team:x"):
            assert (await a.error("POST", f"{BASE}/scopes", 400, json={"path": bad}))["param"]
        by_path = await a.ok("GET", f"{BASE}/scopes/{enc('org:acme/project:app')}")
        assert by_path["id"] == made["id"] and by_path["budgets"] == []
        assert (await a.ok("GET", f"{BASE}/scopes/{made['id']}"))["path"] == made["path"]
        changed = await a.ok("PATCH", f"{BASE}/scopes/{made['id']}", json={"display_name": "New"})
        assert changed["display_name"] == "New"
        await a.error("GET", f"{BASE}/scopes/org:missing", 404)
        listed = await a.ok("GET", f"{BASE}/scopes", params={"kind": "project"})
        assert [s["path"] for s in listed["data"]] == ["org:acme/project:app"]  # type: ignore[attr-defined]
        first = await a.ok("GET", f"{BASE}/scopes", params={"limit": 1})
        assert first["next_cursor"]
        second = await a.ok(
            "GET", f"{BASE}/scopes", params={"limit": 5, "cursor": first["next_cursor"]}
        )
        assert len(second["data"]) >= 1  # type: ignore[arg-type]
        await a.error("GET", f"{BASE}/scopes", 400, params={"kind": "team"})
        await a.error("GET", f"{BASE}/scopes", 400, params={"cursor": "!!"})


# --- keys ------------------------------------------------------------------------------------


@async_test
async def test_key_lifecycle_secret_once_revoke_and_rotate(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        made = await a.ok(
            "POST", f"{BASE}/keys", 201,
            json={"scope_id": PATH, "name": "svc", "allowed_models": ["alpha"],
                  "limits": {"rpm": 30, "per_call_ceiling_nano": 10**9}},
        )  # fmt: skip
        secret = str(made["secret"])
        assert secret.startswith("sg-") and made["limits"] == {
            "rpm": 30,
            "per_call_ceiling_nano": 10**9,
        }
        listing = await a.call("GET", f"{BASE}/keys")
        assert secret not in listing.text and "secret" not in listing.text
        got = await a.ok("GET", f"{BASE}/keys/{made['id']}")
        assert got["revoked"] is False and "secret" not in got
        assert await chat_as(a, secret) == 200
        grace = await a.ok(
            "POST", f"{BASE}/keys/{made['id']}/rotate", 201, json={"grace_seconds": 60}
        )
        assert grace["secret"] != secret and grace["id"] != made["id"]
        assert await chat_as(a, secret) == 200  # still inside the grace period
        assert await chat_as(a, str(grace["secret"])) == 200
        revoked = await a.ok("POST", f"{BASE}/keys/{made['id']}/revoke")
        assert revoked["revoked"] is True
        assert await chat_as(a, secret) == 401  # takes effect without a restart
        live = await a.ok("GET", f"{BASE}/keys", params={"scope_id": PATH})
        ids = [k["id"] for k in live["data"]]  # type: ignore[attr-defined]
        assert made["id"] not in ids and grace["id"] in ids  # revoked keys are hidden by default
        every = await a.ok("GET", f"{BASE}/keys", params={"include_revoked": "true"})
        assert made["id"] in [k["id"] for k in every["data"]]  # type: ignore[attr-defined]
        shown = await a.ok("GET", f"{BASE}/keys", params={"limit": 1})
        assert shown["next_cursor"]


@async_test
async def test_key_update_replaces_what_is_sent_and_keeps_the_rest(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        made = await a.ok(
            "POST", f"{BASE}/keys", 201,
            json={"scope_id": PATH, "allowed_models": ["alpha"], "limits": {"rpm": 5}},
        )  # fmt: skip
        url = f"{BASE}/keys/{made['id']}"
        patched = await a.ok("PATCH", url, json={"name": "renamed", "limits": {"tpm": 100}})
        assert patched["name"] == "renamed" and patched["limits"] == {"tpm": 100}
        assert patched["allowed_models"] == ["alpha"]
        expiring = await a.ok("PATCH", url, json={"expires_at": "2031-01-01T00:00:00Z"})
        assert str(expiring["expires_at"]).startswith("2031-01-01T00:00:00")
        cleared = await a.ok("PATCH", url, json={"expires_at": None})
        assert cleared["expires_at"] is None
        wide = await a.ok("PATCH", url, json={"allowed_models": None})
        assert "allowed_models" not in wide  # null means every configured model
        assert (await a.ok("PATCH", url, json={}))["name"] == "renamed"
        bad = await a.error("PATCH", url, 400, json={"limits": {"rpm": 0}})
        assert bad["param"] == "limits.rpm"


@async_test
async def test_admin_keys_are_not_manageable_through_the_api(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        await a.error("GET", f"{BASE}/keys/{a.key_id}", 404)
        await a.error("POST", f"{BASE}/keys/{a.key_id}/revoke", 404)
        await a.error("POST", f"{BASE}/keys", 404, json={"scope_id": "org:missing"})
        await a.error("POST", f"{BASE}/keys", 400, json={"scope_id": PATH, "limits": {"rpm": "5"}})


# --- budgets, grants, ack-overrun ------------------------------------------------------------


def money(amount: str) -> dict[str, str]:
    return {"amount": amount, "currency": "USD"}


@async_test
async def test_budget_create_update_grant_and_delete_with_money_as_strings(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        url = f"{BASE}/scopes/{enc('org:acme')}/budgets"
        made = await a.ok(
            "POST", url, 201,
            json={"period": "month", "cap": money("20.5"), "soft_pct": 70, "note": "n"},
        )  # fmt: skip
        assert made["cap"] == money("20.500000000") and made["effective_cap"] == made["cap"]
        assert made["spent"] == money("0.000000000") and made["note"] == "n"
        assert made["remaining"] == money("20.500000000") and made["strict_overrun"] is True
        bid = str(made["id"])
        listed = await a.ok("GET", url)
        assert [b["id"] for b in listed["data"]] == [bid]  # type: ignore[attr-defined]
        up = await a.ok("PATCH", f"{BASE}/budgets/{bid}", json={"cap": money("30"), "soft_pct": 90})
        assert up["cap"] == money("30.000000000") and up["soft_pct"] == 90
        grant = await a.ok(
            "POST", f"{BASE}/budgets/{bid}/grants", 201,
            json={"amount": money("5"), "valid_until": "2031-01-01T00:00:00Z", "reason": "launch"},
        )  # fmt: skip
        assert grant["created_by"] == a.key_id and grant["budget_id"] == bid
        after = await a.ok("GET", url)
        assert after["data"][0]["effective_cap"] == money("35.000000000")  # type: ignore[index]
        await a.ok("DELETE", f"{BASE}/budgets/{bid}", 204)
        await a.error("DELETE", f"{BASE}/budgets/{bid}", 404)
        assert (await a.ok("GET", url))["data"] == []


@async_test
async def test_budget_validation_errors(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        url = f"{BASE}/scopes/{enc('org:acme')}/budgets"
        err = await a.error("POST", url, 400, json={"cap": {"amount": "5", "currency": "EUR"}})
        assert err["param"] == "cap.currency"
        for amount in ("-1", "1e3", "5.0000000001", 5):
            err = await a.error(
                "POST", url, 400, json={"cap": {"amount": amount, "currency": "USD"}}
            )
            assert err["code"] == "invalid_request"
        await a.error("POST", f"{BASE}/scopes/org:nope/budgets", 404, json={"cap": money("1")})
        made = await a.ok("POST", url, 201, json={"cap": money("1")})
        grants = f"{BASE}/budgets/{made['id']}/grants"
        body = {"amount": money("1"), "valid_from": "2031-01-02T00:00:00Z",
                "valid_until": "2031-01-01T00:00:00Z", "reason": "r"}  # fmt: skip
        assert (await a.error("POST", grants, 400, json=body))["param"] == "valid_until"
        naive = body | {"valid_from": "2031-01-01T00:00:00", "valid_until": "2031-01-02T00:00:00Z"}
        await a.error("POST", grants, 400, json=naive)
        await a.error("POST", f"{BASE}/budgets/nope/grants", 404, json=body)
        await a.error("POST", f"{BASE}/budgets/{made['id']}/ack-overrun", 400, json={"reason": ""})


@async_test
async def test_a_model_filter_that_matches_no_routed_catalog_id_is_refused(tmp_path: Path) -> None:
    """The filter is a glob over catalog ids: one written against an alias would never apply."""
    async with open_admin(tmp_path) as a:
        url = f"{BASE}/scopes/{enc('org:acme')}/budgets"
        for pattern in ("alpha", "alpha*", "OPENAI/*", "openai/gpt-7-*", ""):
            body = {"cap": money("1"), "model_filter": pattern}
            err = await a.error("POST", url, 400, json=body)
            assert (err["code"], err["param"]) == ("invalid_request", "model_filter")
            assert "catalog id" in str(err["message"])
        assert (await a.ok("GET", url))["data"] == []  # nothing was stored
        for pattern in ("openai/gpt-6-luna", "openai/*", "*haiku*"):
            made = await a.ok("POST", url, 201, json={"cap": money("1"), "model_filter": pattern})
            assert made["model_filter"] == pattern


@async_test
async def test_ack_overrun_clears_the_review_flag(tmp_path: Path) -> None:
    fake = FakeUpstream()
    fake.enqueue(Script(usage=ABSURD_USAGE))  # costs far more than the hold: a breach overrun
    async with open_admin(tmp_path, fake=fake, budget="1") as a:
        assert (await a.stack.chat()).status_code == 200
        url = f"{BASE}/scopes/{a.stack.scope.id}/budgets"
        flagged = (await a.ok("GET", url))["data"][0]  # type: ignore[index]
        assert flagged["review_required_at"] is not None
        status = await a.ok("GET", f"{BASE}/status")
        assert {
            "scope_id": a.stack.scope.id,
            "budget_id": flagged["id"],
            "kind": "overrun_review",
        } in (
            status["budget_alerts"]  # type: ignore[operator]
        )
        refused = await a.stack.chat()
        assert refused.status_code == 402
        assert refused.json()["error"]["details"]["reason"] == "overrun_review"
        acked = await a.ok(
            "POST", f"{BASE}/budgets/{flagged['id']}/ack-overrun", json={"reason": "reviewed"}
        )
        assert acked["review_required_at"] is None
        await a.error("POST", f"{BASE}/budgets/nope/ack-overrun", 404, json={"reason": "r"})


# --- kill switches ---------------------------------------------------------------------------


@async_test
async def test_kill_switch_engage_list_lift_and_readyz(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        assert (await a.stack.client.get("/readyz")).status_code == 200
        made = await a.ok(
            "POST", f"{BASE}/kill-switches", 201, json={"target_kind": "global", "reason": "drill"}
        )
        assert "target_ref" not in made and made["created_by"] == a.key_id
        listed = await a.ok("GET", f"{BASE}/kill-switches")
        assert [k["id"] for k in listed["data"]] == [made["id"]]  # type: ignore[attr-defined]
        ready = await a.stack.client.get("/readyz")
        assert ready.status_code == 503 and ready.json() == {"status": "not_ready"}  # SR-06
        detail = await a.call("GET", "/admin/v1/readiness")
        assert detail.status_code == 503 and "kill_switch_engaged" in detail.json()["reasons"]
        refused = await a.stack.chat()
        assert refused.status_code == 503 and refused.json()["error"]["code"] == "kill_switch"
        await a.ok("DELETE", f"{BASE}/kill-switches/{made['id']}", 204)
        assert (await a.ok("GET", f"{BASE}/kill-switches"))["data"] == []
        assert (await a.stack.client.get("/readyz")).status_code == 200
        await a.error("DELETE", f"{BASE}/kill-switches/nope", 404)


@async_test
async def test_kill_switch_targets_are_validated_and_normalised(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        url = f"{BASE}/kill-switches"
        by_scope = await a.ok(
            "POST", url, 201, json={"target_kind": "scope", "target_ref": PATH, "reason": "r"}
        )
        assert by_scope["target_ref"] == a.stack.scope.id  # a path is stored as the id
        await a.ok(
            "POST",
            url,
            201,
            json={"target_kind": "provider", "target_ref": "openai", "reason": "r"},
        )
        await a.ok(
            "POST",
            url,
            201,
            json={"target_kind": "model", "target_ref": "openai/gpt-6-luna", "reason": "r"},
        )
        bad = [
            {"target_kind": "global", "target_ref": "x"},
            {"target_kind": "scope"},
            {"target_kind": "provider", "target_ref": "nope"},
            {"target_kind": "model", "target_ref": "nope/none"},
            {"target_kind": "other"},
        ]
        for body in bad:
            assert (await a.error("POST", url, 400, json=body | {"reason": "r"}))["code"]
        await a.error(
            "POST", url, 404, json={"target_kind": "key", "target_ref": "nope", "reason": "r"}
        )
        await a.error(
            "POST", url, 404, json={"target_kind": "scope", "target_ref": "org:x", "reason": "r"}
        )


# T-KEY-01
@async_test
async def test_t_key_01_an_empty_allowed_models_is_refused_not_read_as_every_model(
    tmp_path: Path,
) -> None:
    """SR-09: `[]` looks like "no models" but was stored as "every model"; never widen silently."""
    async with open_admin(tmp_path) as a:
        refused = await a.error(
            "POST", f"{BASE}/keys", 400, json={"scope_id": PATH, "allowed_models": []}
        )
        assert refused["code"] == "invalid_request" and refused["param"] == "allowed_models"
        made = await a.ok(
            "POST", f"{BASE}/keys", 201, json={"scope_id": PATH, "allowed_models": ["alpha"]}
        )
        url = f"{BASE}/keys/{made['id']}"
        await a.error("PATCH", url, 400, json={"allowed_models": []})
        still = await a.ok("GET", url)
        assert still["allowed_models"] == ["alpha"]  # the refused PATCH changed nothing
