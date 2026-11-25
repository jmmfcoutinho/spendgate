"""Usage, calls, audit, status, metrics exposure and the audit trail of every admin write."""

from pathlib import Path
from typing import Any

import httpx

from spendgate import package_version
from spendgate.gateway.admin import combine, create_admin_app
from spendgate.gateway.app import create_app
from spendgate.ledger.key_types import AdminScope
from tests.integration.harness import NOW_MS, async_test
from tests.integration.test_admin import BASE, PATH, enc, money
from tests.integration.test_admin_support import open_admin

DAY = 86_400_000


@async_test
async def test_usage_by_service_and_model_matches_the_charged_cost(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        r = await a.stack.chat()
        assert r.status_code == 200
        charged = r.headers["x-spendgate-cost"].split(" ")[0]
        usage = await a.ok("GET", f"{BASE}/usage", params={"by": "user,model", "since": "7d"})
        assert usage["currency"] == "USD" and usage["next_cursor"] is None
        rows: list[dict[str, Any]] = usage["rows"]  # type: ignore[assignment]
        [row] = rows
        assert row["group"]["model"] == "openai/gpt-6-luna"
        assert row["calls"] == 1 and row["cost"]["amount"] == charged
        assert usage["total"] == row["cost"]
        by_scope = await a.ok(
            "GET", f"{BASE}/usage", params={"by": "scope", "scope_id": "org:acme"}
        )
        assert by_scope["rows"][0]["group"] == {"scope": PATH}  # type: ignore[index]
        again = await a.ok("GET", f"{BASE}/usage", params={"model": "other", "basis": "usage"})
        assert again["total"] == money("0.000000000")
        none = await a.ok("GET", f"{BASE}/usage")
        assert none["rows"][0]["calls"] == 1  # type: ignore[index]


@async_test
async def test_usage_errors_name_their_parameter(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        await a.stack.chat()
        old = await a.error("GET", f"{BASE}/usage", 400, params={"since": "10000d"})
        assert old["code"] == "invalid_request" and old["param"] == "since"
        assert (await a.error("GET", f"{BASE}/usage", 404, params={"scope_id": "org:nope"}))["code"]
        assert (await a.error("GET", f"{BASE}/usage", 400, params={"by": "nonsense"}))[
            "param"
        ] == "by"
        assert (await a.error("GET", f"{BASE}/usage", 400, params={"since": "soon"}))[
            "param"
        ] == "since"
        assert (await a.error("GET", f"{BASE}/usage", 400, params={"basis": "x"}))[
            "param"
        ] == "basis"
        assert (await a.error("GET", f"{BASE}/usage", 400, params={"limit": "0"}))[
            "param"
        ] == "limit"
        assert (await a.error("GET", f"{BASE}/usage", 400, params={"tag": "novalue"}))["code"]


@async_test
async def test_usage_pages_with_an_opaque_cursor(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        await a.stack.chat()
        await a.stack.chat(model="alpha-one")
        first = await a.ok("GET", f"{BASE}/usage", params={"by": "deployment", "limit": 1})
        assert first["next_cursor"] and len(first["rows"]) == 1  # type: ignore[arg-type]
        second = await a.ok(
            "GET", f"{BASE}/usage",
            params={"by": "deployment", "limit": 1, "cursor": first["next_cursor"]},
        )  # fmt: skip
        assert second["rows"] != first["rows"]


@async_test
async def test_calls_list_get_and_filters(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        first = await a.stack.chat()
        await a.stack.chat()
        rid = first.headers["x-spendgate-request-id"]
        one = await a.ok("GET", f"{BASE}/calls/{rid}")
        assert one["request_id"] == rid and one["outcome"] == "ok" and one["streamed"] is False
        assert one["charged_nano"] > 0 and one["labels"] == {}  # type: ignore[operator]
        listed = await a.ok("GET", f"{BASE}/calls")
        assert len(listed["data"]) == 2 and listed["next_cursor"] is None  # type: ignore[arg-type]
        page = await a.ok("GET", f"{BASE}/calls", params={"limit": 1})
        assert page["next_cursor"]
        rest = await a.ok("GET", f"{BASE}/calls", params={"cursor": page["next_cursor"]})
        ids = [c["request_id"] for c in page["data"] + rest["data"]]  # type: ignore[operator]
        assert len(set(ids)) == 2
        only = await a.ok("GET", f"{BASE}/calls", params={"scope_id": PATH, "outcome": "ok"})
        assert len(only["data"]) == 2  # type: ignore[arg-type]
        empty = await a.ok("GET", f"{BASE}/calls", params={"outcome": "throttled"})
        assert empty["data"] == []
        await a.error("GET", f"{BASE}/calls/sg_req_nope", 404)
        await a.error("GET", f"{BASE}/calls", 400, params={"outcome": "weird"})


@async_test
async def test_every_admin_write_is_audited_with_actor_and_time(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        scope = await a.ok("POST", f"{BASE}/scopes", 201, json={"path": "org:acme/project:p"})
        await a.ok("PATCH", f"{BASE}/scopes/{scope['id']}", json={"display_name": "P"})
        key = await a.ok("POST", f"{BASE}/keys", 201, json={"scope_id": str(scope["id"])})
        await a.ok("PATCH", f"{BASE}/keys/{key['id']}", json={"name": "k2"})
        await a.ok("PATCH", f"{BASE}/keys/{key['id']}", json={"expires_at": "2031-01-01T00:00:00Z"})
        await a.ok("POST", f"{BASE}/keys/{key['id']}/rotate", 201)
        await a.ok("POST", f"{BASE}/keys/{key['id']}/revoke")
        budget = await a.ok(
            "POST", f"{BASE}/scopes/{enc('org:acme/project:p')}/budgets", 201,
            json={"cap": money("3")},
        )  # fmt: skip
        bid = budget["id"]
        await a.ok("PATCH", f"{BASE}/budgets/{bid}", json={"cap": money("4")})
        await a.ok(
            "POST", f"{BASE}/budgets/{bid}/grants", 201,
            json={"amount": money("1"), "valid_until": "2031-01-01T00:00:00Z", "reason": "r"},
        )  # fmt: skip
        await a.ok("POST", f"{BASE}/budgets/{bid}/ack-overrun", json={"reason": "r"})
        await a.ok("DELETE", f"{BASE}/budgets/{bid}", 204)
        kill = await a.ok(
            "POST", f"{BASE}/kill-switches", 201, json={"target_kind": "global", "reason": "r"}
        )
        await a.ok("DELETE", f"{BASE}/kill-switches/{kill['id']}", 204)
        audit = await a.ok("GET", f"{BASE}/audit", params={"actor": a.key_id, "limit": 500})
        entries = audit["data"]
        actions = {e["action"] for e in entries}  # type: ignore[attr-defined]
        assert actions >= {
            "scope.create", "scope.update", "key.create", "key.update", "key.rotate",
            "key.revoke", "budget.create", "budget.set_cap", "budget.grant", "overrun.ack",
            "budget.delete", "kill.set", "kill.lift",
        }  # fmt: skip
        for e in entries:  # type: ignore[attr-defined]
            assert e["actor"] == a.key_id and e["at"].startswith("2026-11-17T")
            assert "secret" not in str(e) and "sg-" not in str(e)
        grant = next(e for e in entries if e["action"] == "budget.grant")  # type: ignore[attr-defined]
        assert grant["target"] == f"budget:{bid}" and grant["before"] is None
        some = await a.ok("GET", f"{BASE}/audit", params={"limit": 2})
        assert some["next_cursor"] and len(some["data"]) == 2  # type: ignore[arg-type]
        other = await a.ok("GET", f"{BASE}/audit", params={"actor": "someone-else"})
        assert other["data"] == []


@async_test
async def test_status_reports_ledger_catalog_alerts_and_switches(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        a.stack.clock.advance(seconds=5)  # the gateway was built at NOW_MS
        await a.ok(
            "POST", f"{BASE}/kill-switches", 201, json={"target_kind": "global", "reason": "r"}
        )
        s = await a.ok("GET", f"{BASE}/status")
        assert s["version"] == package_version() and s["uptime_seconds"] == 5
        assert s["ledger"] == {"backend": "sqlite", "healthy": True, "pending_holds": 0}
        assert s["catalog"]["entries"] > 0  # type: ignore[index]
        assert len(s["kill_switches"]) == 1  # type: ignore[arg-type]
        a.stack.ledger.close()
        down = await a.ok("GET", f"{BASE}/status")
        assert down["ledger"]["healthy"] is False  # type: ignore[index]


# --- readiness and metrics exposure ------------------------------------------------------------


# T-CLK-03
@async_test
async def test_readyz_warns_on_clock_skew_from_the_database_clock(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        a.stack.clock.set(NOW_MS)  # the harness clock is a fixed 2026 date, the database's is real
        r = await a.call("GET", "/admin/v1/readiness")  # details live behind the admin key (SR-06)
        assert r.status_code == 200
        assert any(w.startswith("clock_skew") for w in r.json()["warnings"])


# T-OBS-01
@async_test
async def test_metrics_need_an_admin_key_with_usage_read_on_a_shared_port(tmp_path: Path) -> None:
    async with open_admin(tmp_path, shared_port=True) as a:
        assert (await a.call("GET", "/metrics", key="")).status_code == 401
        assert (await a.call("GET", "/metrics", key=a.stack.secret)).status_code == 401
        no_read = await a.admin_key(AdminScope.KEYS_WRITE)
        assert (await a.call("GET", "/metrics", key=no_read)).status_code == 403
        ok = await a.call("GET", "/metrics")
        assert ok.status_code == 200 and "spendgate_requests_total" in ok.text


@async_test
async def test_metrics_on_a_separate_admin_port_need_no_key(tmp_path: Path) -> None:
    async with open_admin(tmp_path, shared_port=False) as a:
        r = await a.call("GET", "/metrics", key="")
        assert r.status_code == 200 and "spendgate_requests_total" in r.text
        assert (await a.call("GET", f"{BASE}/status", key="")).status_code == 401  # API still keyed


@async_test
async def test_combined_listener_routes_admin_and_data_and_keeps_the_lifespan(
    tmp_path: Path,
) -> None:
    async with open_admin(tmp_path) as a:
        data_app = create_app(a.stack.pipeline)  # no metrics: the admin app owns /metrics
        both = combine(data_app, create_admin_app(a.ctx, shared_port=True))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=both), base_url="http://x"
        ) as client:
            assert (await client.get("/livez")).status_code == 200
            assert (await client.get(f"{BASE}/status")).status_code == 401  # reached the admin app
            assert (await client.get("/metrics")).status_code == 401
            ok = await client.get(f"{BASE}/status", headers={"Authorization": f"Bearer {a.secret}"})
            assert ok.status_code == 200
        separate = create_app(a.stack.pipeline)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=separate), base_url="http://x"
        ) as client:
            assert (await client.get(f"{BASE}/status")).status_code == 404  # not on the data port
