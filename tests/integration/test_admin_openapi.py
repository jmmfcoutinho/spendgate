"""Conformance to docs/admin-openapi.yaml without schemathesis (not installed): every documented
path and method is routed with the documented scope, and every response a scripted run of calls
produces validates against its schema. The request bodies the script sends validate too."""

import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from spendgate.gateway.admin import BASE, OPERATIONS
from spendgate.ledger.key_types import AdminScope
from tests.fakes import FakeUpstream
from tests.integration.harness import async_test
from tests.integration.test_admin_support import Admin, open_admin

SPEC_FILE = Path(__file__).parents[2] / "docs" / "admin-openapi.yaml"
SPEC: dict[str, Any] = yaml.safe_load(SPEC_FILE.read_text())
METHODS = ("get", "post", "patch", "put", "delete")
FORMATS = FormatChecker()


@FORMATS.checks("date-time", raises=ValueError)
def _date_time(value: object) -> bool:
    return not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is not None


@FORMATS.checks("date", raises=ValueError)
def _date(value: object) -> bool:
    return not isinstance(value, str) or bool(datetime.strptime(value, "%Y-%m-%d"))


def _nodes(node: object, path: str = "") -> list[tuple[str, dict[str, Any]]]:
    """Every mapping in the spec with its path, depth first."""
    if isinstance(node, list):
        return [n for i, item in enumerate(node) for n in _nodes(item, f"{path}[{i}]")]
    if not isinstance(node, dict):
        return []
    found = [(path, node)]
    for key, value in node.items():
        found += _nodes(value, f"{path}/{key}")
    return found


def test_the_spec_parses_without_empty_keys_and_every_description_is_text() -> None:
    """An unquoted comma inside a `{...}` flow mapping splits a description into stray keys
    with no value; quote such descriptions."""
    stray = [f"{path}/{key}" for path, node in _nodes(SPEC) for key, v in node.items() if v is None]
    assert stray == []
    texts = [(p, n) for p, n in _nodes(SPEC) if not p.endswith("/properties")]  # not a field name
    bad = [path for path, node in texts if not isinstance(node.get("description", ""), str)]
    assert bad == []


def documented() -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (m.upper(), path): op
        for path, item in SPEC["paths"].items()
        for m, op in item.items()
        if m in METHODS
    }


def routed() -> dict[tuple[str, str], AdminScope]:
    # `{scope_id:path}` is Starlette's spelling of the documented `{scope_id}`.
    return {(op.method, BASE + re.sub(r":path\}", "}", op.path)): op.scope for op in OPERATIONS}


def validator(schema: dict[str, Any]) -> Draft202012Validator:
    # The components travel with the schema so `#/components/...` references resolve.
    return Draft202012Validator(
        {**schema, "components": SPEC["components"]}, format_checker=FORMATS
    )


def response_schema(op: dict[str, Any], status: int) -> dict[str, Any] | None:
    found = op["responses"].get(str(status)) or op["responses"]["default"]
    if "$ref" in found:
        found = SPEC["components"]["responses"][found["$ref"].rsplit("/", 1)[1]]
    content = found.get("content", {}).get("application/json")
    schema: dict[str, Any] | None = None if content is None else content["schema"]
    return schema


def test_every_documented_path_and_method_is_routed_with_its_documented_scope() -> None:
    docs, mine = documented(), routed()
    assert set(docs) == set(mine)  # no operation missing, none invented
    for key, op in docs.items():
        assert AdminScope(op["x-required-scope"]) is mine[key], key
    assert len(docs) == 25


class Run:
    """One scripted call: fills the template, sends it, validates request and response."""

    def __init__(self, admin: Admin) -> None:
        self.admin = admin
        self.docs = documented()
        self.seen: set[tuple[str, str]] = set()

    async def hit(  # noqa: PLR0913, PLR0917
        self,
        method: str,
        template: str,
        expect: int,
        params: dict[str, str] | None = None,
        key: str | None = None,
        query: dict[str, str] | None = None,
        valid: bool = True,
        **kw: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        op = self.docs[(method, template)]
        path = template.format(**{k: quote(v, safe="") for k, v in (params or {}).items()})
        if "json" in kw and valid:
            self._check_request(op, kw["json"])
        r = await self.admin.call(method, path, key=key, params=query, **kw)
        assert r.status_code == expect, (method, template, r.text)
        schema = response_schema(op, expect)
        body = r.json() if r.content else None
        if schema is None:
            assert body is None, (method, template)
        else:
            errors = sorted(validator(schema).iter_errors(body), key=str)
            assert not errors, (method, template, expect, errors[0].message, body)
        self.seen.add((method, template))
        return body

    def _check_request(self, op: dict[str, Any], body: object) -> None:
        schema = op["requestBody"]["content"]["application/json"]["schema"]
        errors = list(validator(schema).iter_errors(body))
        assert not errors, (errors[0].message, body)


def usd(amount: str) -> dict[str, str]:
    return {"amount": amount, "currency": "USD"}


@async_test
async def test_responses_validate_against_the_openapi_schemas(tmp_path: Path) -> None:
    fake = FakeUpstream()
    async with open_admin(tmp_path, fake=fake) as a:
        run = Run(a)
        p = f"{BASE}"
        chat = await a.stack.chat()
        rid = chat.headers["x-spendgate-request-id"]
        scope = await run.hit(
            "POST", f"{p}/scopes", 201, json={"path": "org:acme/project:app", "display_name": "App"}
        )
        await run.hit("GET", f"{p}/scopes", 200)
        await run.hit("GET", f"{p}/scopes/{{scope_id}}", 200, {"scope_id": "org:acme/project:app"})
        await run.hit(
            "PATCH",
            f"{p}/scopes/{{scope_id}}",
            200,
            {"scope_id": scope["id"]},
            json={"display_name": "A"},
        )
        key = await run.hit("POST", f"{p}/keys", 201, json={
            "scope_id": scope["id"], "name": "n", "allowed_models": ["alpha"],
            "limits": {"rpm": 5, "per_call_ceiling_nano": 7},
            "session_defaults": {"max_steps": 3}, "expires_at": "2031-01-01T00:00:00Z",
        })  # fmt: skip
        kid = {"key_id": key["id"]}
        await run.hit("GET", f"{p}/keys", 200)
        await run.hit("GET", f"{p}/keys/{{key_id}}", 200, kid)
        await run.hit(
            "PATCH", f"{p}/keys/{{key_id}}", 200, kid, json={"name": "m", "expires_at": None}
        )
        rotated = await run.hit(
            "POST", f"{p}/keys/{{key_id}}/rotate", 201, kid, json={"grace_seconds": 5}
        )
        await run.hit("POST", f"{p}/keys/{{key_id}}/revoke", 200, {"key_id": rotated["id"]})
        sp = {"scope_id": "org:acme/project:app"}
        budget = await run.hit(
            "POST", f"{p}/scopes/{{scope_id}}/budgets", 201, sp,
            json={"period": "month", "cap": usd("9.5"), "soft_pct": 60,
                  "model_filter": "openai/gpt-6-*", "strict_overrun": False, "note": "n"},
        )  # fmt: skip
        bp = {"budget_id": budget["id"]}
        await run.hit("GET", f"{p}/scopes/{{scope_id}}/budgets", 200, sp)
        await run.hit(
            "PATCH", f"{p}/budgets/{{budget_id}}", 200, bp, json={"cap": usd("11"), "soft_pct": 70}
        )
        await run.hit("POST", f"{p}/budgets/{{budget_id}}/grants", 201, bp, json={
            "amount": usd("2"), "valid_from": "2026-11-17T00:00:00Z",
            "valid_until": "2031-01-01T00:00:00Z", "reason": "x",
        })  # fmt: skip
        await run.hit(
            "POST", f"{p}/budgets/{{budget_id}}/ack-overrun", 200, bp, json={"reason": "r"}
        )
        await run.hit("GET", f"{p}/scopes/{{scope_id}}", 200, {"scope_id": scope["id"]})
        await run.hit("DELETE", f"{p}/budgets/{{budget_id}}", 204, bp)
        switch = await run.hit("POST", f"{p}/kill-switches", 201, json={
            "target_kind": "scope", "target_ref": "org:acme/project:app", "reason": "r",
        })  # fmt: skip
        await run.hit("GET", f"{p}/kill-switches", 200)
        await run.hit(
            "DELETE", f"{p}/kill-switches/{{switch_id}}", 204, {"switch_id": switch["id"]}
        )
        await run.hit("GET", f"{p}/usage", 200, query={"by": "service,model", "since": "7d"})
        by_all = "scope,service,key,provider,day,tag"
        await run.hit("GET", f"{p}/usage", 200, query={"by": by_all})
        await run.hit("GET", f"{p}/calls", 200)
        call = await run.hit("GET", f"{p}/calls/{{request_id}}", 200, {"request_id": rid})
        assert call["request_id"] == rid
        await run.hit("GET", f"{p}/audit", 200)
        await run.hit("GET", f"{p}/status", 200)
        await run.hit("GET", f"{p}/readiness", 200)
        assert run.seen == set(run.docs)  # the script touched every operation

        # Error responses validate against the shared Error schema.
        await run.hit("GET", f"{p}/status", 401, key="")
        read_only = await a.admin_key(AdminScope.USAGE_READ)
        await run.hit("POST", f"{p}/scopes", 403, key=read_only, json={"path": "org:z"})
        await run.hit("GET", f"{p}/scopes/{{scope_id}}", 404, {"scope_id": "org:missing"})
        await run.hit("POST", f"{p}/scopes", 409, json={"path": "org:acme"})
        await run.hit("POST", f"{p}/scopes", 400, json={"path": "bad path"}, valid=False)
        await run.hit("GET", f"{p}/usage", 400, query={"since": "10000d"})
        await run.hit("GET", f"{p}/usage", 404, query={"scope_id": "org:missing"})
        await run.hit("GET", f"{p}/calls/{{request_id}}", 404, {"request_id": "nope"})


@async_test
async def test_a_request_the_schema_forbids_is_refused(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        run = Run(a)
        op = run.docs[("POST", f"{BASE}/budgets/{{budget_id}}/grants")]
        schema = op["requestBody"]["content"]["application/json"]["schema"]
        bad = {"amount": usd("1"), "valid_until": "2031-01-01T00:00:00Z", "reason": ""}
        assert list(validator(schema).iter_errors(bad))  # the spec forbids an empty reason
        budget = await run.hit(
            "POST",
            f"{BASE}/scopes/{{scope_id}}/budgets",
            201,
            {"scope_id": "org:acme"},
            json={"cap": usd("1")},
        )
        err = await a.error("POST", f"{BASE}/budgets/{budget['id']}/grants", 400, json=bad)
        assert err["code"] == "invalid_request"
