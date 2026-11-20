# ruff: noqa: PLR0912, PLR0915
"""Target 7: the admin API, fuzzed from `docs/admin-openapi.yaml` (SR-07 time overflow).

schemathesis is not installed, so requests are generated from the spec's own schemas
(`schema_strategies.py`) and sent to the in-process ASGI admin app through `httpx.ASGITransport`,
over a real SQLite ledger, with the application exception left to propagate (an unhandled exception
is a failure, exactly like a 500).

Per request: any of the 25 operations; path ids drawn from the ids the run has created plus junk;
query values from the parameter schemas plus hostile times / cursors / enums; bodies valid,
hostile (boundary numbers, wrong types, unknown fields) or not JSON at all; the key is the
all-scope admin key, one of the three single-scope keys, a data-plane key, garbage, or absent.

Invariants: no 5xx and no exception (except the documented 503 of `/readiness`); every response
validates against its documented schema (errors against `Error`); a `secret` appears only in the
`201` of key create and rotate; no key-shaped string, key hash or pepper appears anywhere else; the
scope check holds for each single-scope key (right scope: never 401/403; wrong scope: exactly 403;
no/invalid/data-plane key: exactly 401).
"""

import asyncio
import json
import re
import tempfile
from collections.abc import Awaitable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from hypothesis import strategies as st

from spendgate.gateway.admin import BASE
from spendgate.gateway.admin_http import encode_cursor
from spendgate.ledger.key_types import AdminScope
from tests.fakes import FakeUpstream
from tests.fuzz.conftest import Case, run_fuzz
from tests.fuzz.schema_strategies import hostile, junk, valid
from tests.integration.harness import NOW_MS, PEPPER
from tests.integration.test_admin_openapi import SPEC, documented, response_schema, validator
from tests.integration.test_admin_support import Admin, open_admin

_KEY_SHAPE = re.compile(r"sga?-[0-9a-f]{12}-[0-9a-f]{48}")
_SECRET_OK = {("POST", "/admin/v1/keys"), ("POST", "/admin/v1/keys/{key_id}/rotate")}
_OPS = sorted(documented().items())
_REQUIRED = {
    "usage:read": AdminScope.USAGE_READ,
    "keys:write": AdminScope.KEYS_WRITE,
    "budgets:write": AdminScope.BUDGETS_WRITE,
}


class Rig:
    """One admin app over one ledger on a private event loop; pools of ids created so far."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.tmp = tempfile.TemporaryDirectory()
        self.cm: AbstractAsyncContextManager[Admin] = open_admin(
            Path(self.tmp.name), fake=FakeUpstream()
        )
        self.admin: Admin = self.run(self.cm.__aenter__())
        self.single = {s: self.run(self.admin.admin_key(s)) for s in AdminScope}
        self.pools: dict[str, list[str]] = {
            "scope_id": [self.admin.stack.scope.id],
            "key_id": [self.admin.key_id],
            "budget_id": [self.admin.stack.budget.id],
            "switch_id": [],
            "request_id": [],
            "cursor": [],
        }
        chat = self.run(self.admin.stack.chat())
        self.pools["request_id"].append(chat.headers.get("x-spendgate-request-id", "x"))
        self.calls = 0
        self.statuses: dict[int, int] = {}

    def run[T](self, awaitable: Awaitable[T]) -> T:
        return self.loop.run_until_complete(awaitable)

    def close(self) -> None:
        self.run(self.cm.__aexit__(None, None, None))
        self.loop.close()
        self.tmp.cleanup()

    def harvest(self, method: str, template: str, body: Any) -> None:  # noqa: ANN401
        if not isinstance(body, dict):
            return
        if isinstance(body.get("next_cursor"), str):
            self.pools["cursor"].append(body["next_cursor"])
        ident = body.get("id")
        if not isinstance(ident, str):
            return
        target = {
            "/admin/v1/scopes": "scope_id",
            "/admin/v1/keys": "key_id",
            "/admin/v1/keys/{key_id}/rotate": "key_id",
            "/admin/v1/scopes/{scope_id}/budgets": "budget_id",
            "/admin/v1/kill-switches": "switch_id",
        }.get(template)
        if method == "POST" and target:
            self.pools[target].append(ident)
            if target == "scope_id" and isinstance(body.get("path"), str):
                self.pools[target].append(body["path"])
        for name in self.pools:
            del self.pools[name][:-300]  # keep the pools bounded


_rig: Rig | None = None


def rig() -> Rig:
    global _rig  # noqa: PLW0603 - one app per process, closed by the test
    if _rig is None:
        _rig = Rig()
    return _rig


# --- request generation ------------------------------------------------------------------------

_TIMES = st.one_of(
    st.sampled_from(
        [
            "90m",
            "7d",
            "0s",
            "999999w",
            "1d2h",
            "-5d",
            "1.5h",
            "",
            "now",
            "9" * 30 + "d",
            "\u00b2d",
            "0001-01-01T00:00:00+14:00",
            "0001-01-01T00:00:00+00:00",
            "9999-12-31T23:59:59-14:00",
            "9999-12-31T23:59:59+00:00",
            "2026-11-17T00:00:00Z",
            "2026-11-17T00:00:00",
            "2026-13-45T99:99:99Z",
            "2026-11-17T00:00:00+25:00",
            "10000-01-01T00:00:00Z",
            "2026-11-17",
            "\x00",
        ]
    ),
    st.datetimes(
        min_value=__import__("datetime").datetime(1, 1, 1),
        max_value=__import__("datetime").datetime(9999, 12, 31),
        timezones=st.timezones(),
    ).map(lambda d: d.isoformat()),
    st.text(max_size=30),
)
_BAD_CURSORS = st.one_of(
    st.text(max_size=40),
    st.sampled_from(
        ["", "e30", "W10", "WzEsMl0", "bnVsbA", "W251bGxd", "A" * 5000, "%%%", "W1wiYVwiLDFd"]
    ),
    st.lists(
        st.one_of(st.integers(-(2**70), 2**70), st.text(max_size=10), st.none()), max_size=4
    ).map(lambda p: encode_cursor(*[x for x in p if x is not None])),
)


class Pick(int):
    """An index into a pool of ids; resolved when the request is built (strategies cannot read
    state that changes between examples: Hypothesis would see them as flaky)."""


_PICK = st.integers(0, 10**6).map(Pick)


def resolve(value: Any, pool: list[str]) -> Any:  # noqa: ANN401
    return (pool[value % len(pool)] if pool else "unknown") if isinstance(value, Pick) else value


def _query_value(
    name: str, schema: dict[str, Any], pools: dict[str, list[str]]
) -> st.SearchStrategy[Any]:
    if name in ("since", "until"):
        return _TIMES
    if name == "cursor":
        return st.one_of(_PICK, _BAD_CURSORS)
    if name == "limit":
        return st.one_of(st.integers(-5, 600), st.sampled_from(["", "x", "1e3", str(2**70)]))
    if name == "by":
        return st.lists(st.sampled_from(schema["items"]["enum"]) | st.text(max_size=8), max_size=8)
    if name in pools:
        return _PICK | st.text(max_size=20)
    return st.one_of(
        valid(schema, SPEC), junk.map(str), st.sampled_from(["", "x", "a=b", "a" * 300])
    )


def _resolve_param(raw: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in raw:
        return SPEC["components"]["parameters"][raw["$ref"].rsplit("/", 1)[1]]  # type: ignore[no-any-return]
    return raw


def _path_value(name: str) -> st.SearchStrategy[Any]:
    """Mostly an id that exists (`one_of` would merge equal branches, so the weight is explicit)."""
    other = st.text(max_size=30) | st.sampled_from(
        ["", "..", ".", "%2F", "a/b", "org:x/project:y", "\x00", "\u00f1" * 200]
    )
    return st.tuples(st.integers(0, 9), _PICK, other).map(lambda t: t[1] if t[0] < 7 else t[2])


Payload = tuple[Any, str, bool]  # (document or raw bytes or None, content type, is a document)


def _body(op: dict[str, Any]) -> st.SearchStrategy[Payload]:
    content = op.get("requestBody", {}).get("content", {}).get("application/json")
    if content is None:
        return st.just((None, "application/json", False))
    schema = content["schema"]
    return st.one_of(
        valid(schema, SPEC).map(lambda d: (d, "application/json", True)),
        valid(schema, SPEC).map(lambda d: (d, "application/json", True)),
        valid(schema, SPEC).map(lambda d: (d, "application/json", True)),
        hostile(schema, SPEC).map(lambda d: (d, "application/json", True)),
        hostile(schema, SPEC).map(lambda d: (d, "application/json", True)),
        junk.map(lambda d: (d, "application/json", True)),
        st.binary(max_size=40).map(lambda b: (b, "application/json", False)),
        st.sampled_from(
            [
                (None, "application/json", False),
                (b"{}", "text/plain", False),
                (b"[]", "application/json", False),
                (b"{", "application/json", False),
                (b"[" * 5000, "application/json", False),
            ]
        ),
    )


def _encode(doc: Any, pools: dict[str, list[str]], pick: Pick, link: bool) -> bytes:  # noqa: ANN401
    """The body bytes; with `link`, ids in the document are swapped for ones that exist."""
    if link and isinstance(doc, dict):
        for key, pool in (
            ("scope_id", "scope_id"),
            ("target_ref", "scope_id"),
            ("path", "scope_id"),
        ):
            if isinstance(doc.get(key), str) and key != "path":
                doc[key] = resolve(pick, pools[pool])
    return json.dumps(doc, allow_nan=True).encode()


# --- the check ---------------------------------------------------------------------------------


def _walk_secret(node: Any) -> bool:  # noqa: ANN401
    if isinstance(node, dict):
        return any(
            k in ("secret", "secret_hash", "pepper") or _walk_secret(v) for k, v in node.items()
        )
    if isinstance(node, list):
        return any(_walk_secret(v) for v in node)
    return False


def check_request(data: st.DataObject) -> None:
    r = rig()
    (method, template), op = data.draw(st.sampled_from(_OPS))
    pools = r.pools
    path = template
    shifted = (
        False  # an empty, dot or slashed segment is a different URL (the unrouted-admin fallback)
    )
    for name in re.findall(r"{(\w+)}", template):
        value = resolve(data.draw(_path_value(name)), pools[name])
        shifted = shifted or value in ("", ".", "..") or "/" in value
        path = path.replace("{" + name + "}", quote(value, safe=""))
    params: list[tuple[str, Any]] = []
    for raw in op.get("parameters", []) + SPEC["paths"][template].get("parameters", []):
        param = _resolve_param(raw)
        if param["in"] == "query" and data.draw(st.integers(0, 3)) > 0:
            drawn = data.draw(_query_value(param["name"], param["schema"], pools))
            pool = pools.get(param["name"], pools["cursor"])
            params += [
                (param["name"], resolve(v, pool))
                for v in (drawn if isinstance(drawn, list) else [drawn])
            ]
    payload, content_type, is_doc = data.draw(_body(op))
    body = (
        _encode(payload, pools, data.draw(_PICK), data.draw(st.booleans())) if is_doc else payload
    )
    mode = data.draw(
        st.sampled_from(
            ["all"] * 12
            + ["usage:read", "keys:write", "budgets:write"] * 2
            + ["none", "data_plane", "garbage"]
        )
    )
    headers = {"Content-Type": content_type}
    single = _scope_of(mode)
    secret = {"all": r.admin.secret, "data_plane": r.admin.stack.secret}.get(mode) or (
        r.single[single] if single is not None else None
    )
    if mode == "garbage":
        junk_key = st.text(st.characters(codec="latin-1"), max_size=70) | st.sampled_from(
            ["sga-" + "0" * 12 + "-" + "0" * 48, "sga-xyz", "Bearer"]
        )
        headers["Authorization"] = ("Bearer " + data.draw(junk_key)).encode("latin-1")  # type: ignore[assignment]
    elif secret is not None:
        headers["Authorization"] = f"Bearer {secret}"
    r.calls += 1
    response: httpx.Response = r.run(
        r.admin.client.request(
            method,
            BASE + path.removeprefix(BASE),
            params=params or None,
            content=body,
            headers=headers,
        )
    )
    r.statuses[response.status_code] = r.statuses.get(response.status_code, 0) + 1
    where = f"{method} {template} as {mode} -> {response.status_code}: {response.text[:300]}"

    # 1. the scope check
    required = _REQUIRED[op["x-required-scope"]]
    if shifted:
        assert response.status_code < 500 or response.status_code == 503, where
        return
    if mode in ("none", "data_plane", "garbage"):
        assert response.status_code == 401, where
    elif mode == "all" or _scope_of(mode) is required:
        assert response.status_code not in (401, 403), where
    else:
        assert response.status_code == 403, where
    # 2. no crash
    assert response.status_code < 500 or (
        template.endswith("/readiness") and response.status_code == 503
    ), where
    # 3. schema
    schema = response_schema(op, response.status_code)
    if response.status_code < 300 and str(response.status_code) not in op["responses"]:
        raise AssertionError("undocumented success status: " + where)
    if schema is None:
        assert not response.content, where
        parsed: Any = None
    else:
        assert response.headers["content-type"].startswith("application/json"), where
        parsed = response.json()
        errors = sorted(validator(schema).iter_errors(parsed), key=str)
        assert not errors, f"{errors[0].message} | {where}"
    # 4. secrets
    allowed = (method, template) in _SECRET_OK and response.status_code == 201
    if not allowed:
        assert not _walk_secret(parsed), where
        assert not _KEY_SHAPE.search(response.text), where
        assert PEPPER.decode() not in response.text, where
    else:
        assert _KEY_SHAPE.search(response.text), where
    if response.status_code < 300:
        r.harvest(method, template, parsed)


def _scope_of(mode: str) -> AdminScope | None:
    return {
        "usage:read": AdminScope.USAGE_READ,
        "keys:write": AdminScope.KEYS_WRITE,
        "budgets:write": AdminScope.BUDGETS_WRITE,
    }.get(mode)


def test_fuzz_admin_api() -> None:
    global _rig  # noqa: PLW0603
    try:
        report = run_fuzz(
            "admin_api", [Case("operations_x_keys_x_bodies", st.data(), check_request)]
        )
        r = rig()
        print("ADMIN-STATUSES", dict(sorted(r.statuses.items())), "calls", r.calls)
        assert report["examples"] > 0
    finally:
        if _rig is not None:
            _rig.close()
            _rig = None


_ = NOW_MS
