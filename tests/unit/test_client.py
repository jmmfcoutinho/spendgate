"""Client helpers: headers, session context, typed errors, no-SDK import."""

import asyncio
import json
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime

import anthropic as anthropic_sdk
import httpx
import httpx2
import openai as openai_sdk
import pytest

import spendgate.client as sg
from spendgate.core.errors import ErrorCode, LimitName
from spendgate.core.money import Money

BASE = "http://gateway.test/v1"
KEY = "sg-test-secret"

Seen = list[httpx.Request]


def _ok(seen: Seen) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "x", "object": "list", "data": [], "model": "m"})

    return handler


def _error(
    code: str,
    shape: str,
    details: dict[str, object] | None = None,
    status: int = 402,
    headers: dict[str, str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    obj: dict[str, object] = {"type": "x", "code": code, "message": f"msg {code}", "param": None}
    if details is not None:
        obj["details"] = details
    body = {"type": "error", "error": obj} if shape == "anthropic" else {"error": obj}
    merged = {"x-spendgate-request-id": "sg_req_1", "x-should-retry": "false", **(headers or {})}
    return lambda _req: httpx.Response(status, json=body, headers=merged)


def _to_httpx2(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[httpx2.Request], httpx2.Response]:
    def adapted(request: httpx2.Request) -> httpx2.Response:
        twin = httpx.Request(
            request.method,
            str(request.url),
            headers=list(request.headers.multi_items()),
            content=request.read(),
        )
        reply = handler(twin)
        return httpx2.Response(
            reply.status_code, headers=list(reply.headers.multi_items()), content=reply.content
        )

    return adapted


def _openai(handler: Callable[[httpx.Request], httpx.Response], **kw: object) -> openai_sdk.OpenAI:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return sg.openai(BASE, KEY, http_client=http, max_retries=0, **kw)  # type: ignore[call-overload,no-any-return]


def _anthropic(
    handler: Callable[[httpx.Request], httpx.Response], **kw: object
) -> anthropic_sdk.Anthropic:
    # Anthropic SDK 2.x is built on httpx2, so it needs an httpx2 client and transport.
    http = httpx2.Client(transport=httpx2.MockTransport(_to_httpx2(handler)))
    return sg.anthropic(BASE, KEY, http_client=http, max_retries=0, **kw)  # type: ignore[call-overload,no-any-return]


def _call_openai(c: openai_sdk.OpenAI) -> None:
    c.models.list()


def _call_anthropic(c: anthropic_sdk.Anthropic) -> None:
    c.models.list()


def test_openai_sync_sets_auth_and_attribution_headers() -> None:
    seen: Seen = []
    c = _openai(_ok(seen), session="run-1", user="u-42", tags=("import", "batch"))
    _call_openai(c)
    h = seen[0].headers
    assert h["authorization"] == f"Bearer {KEY}"
    assert (h["x-spendgate-session"], h["x-spendgate-user"]) == ("run-1", "u-42")
    assert h["x-spendgate-tags"] == "import,batch"


def test_anthropic_sync_sets_bearer_key_and_attribution_headers() -> None:
    seen: Seen = []
    c = _anthropic(_ok(seen), user="u-1", tags=["a"])
    _call_anthropic(c)
    h = seen[0].headers
    assert h["authorization"] == f"Bearer {KEY}"
    assert (h["x-spendgate-user"], h["x-spendgate-tags"]) == ("u-1", "a")
    assert "x-spendgate-session" not in h


def test_async_clients_set_headers_and_session() -> None:
    seen: list[tuple[str, str, str]] = []
    listing = {"data": [], "object": "list", "has_more": False}

    async def oa_handler(request: httpx.Request) -> httpx.Response:
        h = request.headers
        seen.append((h["x-spendgate-session"], h["x-spendgate-user"], h["authorization"]))
        return httpx.Response(200, json=listing)

    async def an_handler(request: httpx2.Request) -> httpx2.Response:
        h = request.headers
        seen.append((h["x-spendgate-session"], h["x-spendgate-user"], h["authorization"]))
        return httpx2.Response(200, json=listing)

    async def run() -> None:
        oa_http = httpx.AsyncClient(transport=httpx.MockTransport(oa_handler))
        an_http = httpx2.AsyncClient(transport=httpx2.MockTransport(an_handler))
        oa = sg.openai(BASE, KEY, async_=True, user="u", http_client=oa_http)
        an = sg.anthropic(BASE, KEY, async_=True, user="u", http_client=an_http)
        assert isinstance(oa, openai_sdk.AsyncOpenAI)
        assert isinstance(an, anthropic_sdk.AsyncAnthropic)
        with sg.session("s-async"):
            await oa.models.list()
            await an.models.list()

    asyncio.run(run())
    assert seen == [("s-async", "u", f"Bearer {KEY}"), ("s-async", "u", f"Bearer {KEY}")]


def test_default_clients_are_the_real_sdk_types() -> None:
    assert isinstance(sg.openai(BASE, KEY), openai_sdk.OpenAI)
    assert isinstance(sg.anthropic(BASE, KEY), anthropic_sdk.Anthropic)
    assert isinstance(sg.openai(BASE, KEY, async_=True), openai_sdk.AsyncOpenAI)


def test_session_context_applies_nests_and_restores() -> None:
    seen: Seen = []
    c = _openai(_ok(seen), session="static")
    _call_openai(c)
    with sg.session("outer"):
        _call_openai(c)
        with sg.session("inner"):
            _call_openai(c)
        _call_openai(c)
    _call_openai(c)
    assert [r.headers["x-spendgate-session"] for r in seen] == [
        "static",
        "outer",
        "inner",
        "outer",
        "static",
    ]
    assert sg.current_session() is None


def test_session_context_follows_each_asyncio_task() -> None:
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)  # let the tasks interleave
        seen[request.url.params["who"]] = request.headers.get("x-spendgate-session", "none")
        return httpx.Response(200, json={"data": [], "object": "list", "has_more": False})

    async def worker(c: openai_sdk.AsyncOpenAI, name: str) -> None:
        with sg.session(f"sess-{name}"):
            await asyncio.sleep(0)
            await c.models.list(extra_query={"who": name})

    async def run() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        c = sg.openai(BASE, KEY, async_=True, http_client=http)
        await asyncio.gather(*(worker(c, n) for n in ("a", "b", "c")))
        await c.models.list(extra_query={"who": "outside"})

    asyncio.run(run())
    assert seen == {"a": "sess-a", "b": "sess-b", "c": "sess-c", "outside": "none"}


@pytest.mark.parametrize("arg", ["budget", "max_steps", "max_seconds", "idle_seconds"])
def test_proxy_mode_refuses_session_limit_arguments(arg: str) -> None:
    value: object = "2 USD" if arg == "budget" else 5
    with pytest.raises(ValueError, match="session_defaults"), sg.session("s", **{arg: value}):  # type: ignore[arg-type]
        pytest.fail("must not enter the block")


def test_library_mode_can_reuse_activate_session() -> None:
    ctx = sg.SessionContext("lib", budget="2 USD", max_steps=200)
    with sg.activate_session(ctx):
        assert sg.current_session() == ctx
    assert sg.current_session() is None


def test_proxy_client_refuses_a_leaked_limit_context() -> None:
    c = _openai(_ok([]))
    with sg.activate_session(sg.SessionContext("lib", max_steps=3)), pytest.raises(ValueError):
        _call_openai(c)


@pytest.mark.parametrize("bad", ["a\r\nX-Evil: 1", "a\nb", "", "x" * 129, "sp ace", "a,b"])
def test_bad_labels_are_refused_locally(bad: str) -> None:
    with pytest.raises(ValueError):
        sg.openai(BASE, KEY, session=bad)
    with pytest.raises(ValueError):
        sg.anthropic(BASE, KEY, user=bad)
    with pytest.raises(ValueError), sg.session(bad):
        pytest.fail("must not enter")


@pytest.mark.parametrize(
    "tags", [("a\r\nb",), ("Upper",), ("x" * 33,), ("",), tuple(f"t{i}" for i in range(11))]
)
def test_bad_tags_are_refused_locally(tags: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        sg.openai(BASE, KEY, tags=tags)


def test_caller_headers_and_http_client_are_kept() -> None:
    seen: Seen = []
    c = _openai(_ok(seen), user="u", default_headers={"X-App": "cv"})
    _call_openai(c)
    assert seen[0].headers["x-app"] == "cv"
    assert seen[0].headers["x-spendgate-user"] == "u"


CASES: list[tuple[str, type[sg.SpendgateError], dict[str, object], int]] = [
    (
        "budget_exhausted",
        sg.BudgetExhausted,
        {"scope": "org:acme/project:app", "remaining": "0.000000000 EUR"},
        402,
    ),
    (
        "budget_period_exhausted",
        sg.BudgetExhausted,
        {"scope": "s", "remaining": "1.500000000 EUR", "period_resets_at": "2026-12-01T00:00:00Z"},
        402,
    ),
    ("rate_limited", sg.RateLimited, {"limit": "rpm", "retry_after_seconds": 2}, 429),
    (
        "upstream_throttled",
        sg.UpstreamThrottled,
        {"provider": "openai", "retry_after_seconds": 7},
        429,
    ),
    ("model_not_priced", sg.ModelNotPriced, {}, 404),
    ("session_limit_reached", sg.SessionLimitReached, {"limit": "steps"}, 402),
    ("kill_switch", sg.KillSwitch, {}, 503),
    ("ledger_unavailable", sg.LedgerUnavailable, {"retry_after_seconds": 5}, 503),
]


@pytest.mark.parametrize("shape", ["openai", "anthropic"])
@pytest.mark.parametrize(("code", "cls", "details", "status"), CASES)
def test_each_error_code_becomes_its_typed_exception(
    shape: str, code: str, cls: type[sg.SpendgateError], details: dict[str, object], status: int
) -> None:
    handler = _error(code, shape, details, status)
    with pytest.raises(sg.SpendgateError) as info, sg.raise_for_spendgate():
        if shape == "openai":
            _call_openai(_openai(handler))
        else:
            _call_anthropic(_anthropic(handler))
    err = info.value
    assert type(err) is cls
    assert err.code == code
    assert err.message == f"msg {code}"
    assert err.status_code == status
    assert err.request_id == "sg_req_1"
    assert dict(err.details) == details
    assert isinstance(err.__cause__, (openai_sdk.APIStatusError, anthropic_sdk.APIStatusError))


@pytest.mark.parametrize("shape", ["openai", "anthropic"])
def test_error_details_are_exposed_as_typed_properties(shape: str) -> None:
    def fetch(code: str, details: dict[str, object], status: int) -> sg.SpendgateError:
        h = _error(code, shape, details, status)
        with pytest.raises(sg.SpendgateError) as info, sg.raise_for_spendgate():
            if shape == "openai":
                _call_openai(_openai(h))
            else:
                _call_anthropic(_anthropic(h))
        return info.value

    budget = fetch(
        "budget_period_exhausted",
        {
            "scope": "a/b",
            "remaining": "1.500000000 EUR",
            "period_resets_at": "2026-12-01T00:00:00Z",
        },
        402,
    )
    assert isinstance(budget, sg.BudgetExhausted)
    assert budget.scope == "a/b"
    assert budget.remaining == Money.from_amount("1.5", "EUR")
    assert budget.period_resets_at == datetime(2026, 12, 1, tzinfo=UTC)
    limited = fetch("rate_limited", {"limit": "rpm", "retry_after_seconds": 2}, 429)
    assert isinstance(limited, sg.RateLimited)
    assert limited.retry_after == 2
    session = fetch("session_limit_reached", {"limit": "time"}, 402)
    assert isinstance(session, sg.SessionLimitReached)
    assert session.limit is LimitName.TIME


def test_retry_after_header_backs_up_missing_detail() -> None:
    h = _error("rate_limited", "openai", {"limit": "tpm"}, 429, {"retry-after": "3"})
    with pytest.raises(sg.RateLimited) as info, sg.raise_for_spendgate():
        _call_openai(_openai(h))
    assert info.value.retry_after == 3


def test_other_spendgate_codes_use_the_generic_error() -> None:
    h = _error(ErrorCode.MODEL_NOT_ALLOWED, "openai", {"param": "x"}, 403)
    with pytest.raises(sg.SpendgateError) as info, sg.raise_for_spendgate():
        _call_openai(_openai(h))
    assert type(info.value) is sg.SpendgateError
    assert info.value.code == "model_not_allowed"


def test_unknown_code_with_request_id_is_generic_but_provider_errors_pass_through() -> None:
    new = _error("brand_new_code", "anthropic", None, 418)
    with pytest.raises(sg.SpendgateError) as info, sg.raise_for_spendgate():
        _call_anthropic(_anthropic(new))
    assert info.value.code == "brand_new_code"

    body = {"error": {"type": "x", "code": "rate_limit_exceeded", "message": "provider"}}
    plain = httpx.Response(429, json=body)
    with pytest.raises(openai_sdk.RateLimitError), sg.raise_for_spendgate():
        _call_openai(_openai(lambda _r: plain))


def test_from_sdk_error_returns_none_for_non_sdk_errors() -> None:
    assert sg.from_sdk_error(ValueError("x")) is None
    assert sg.from_sdk_error(object()) is None


def test_importing_client_does_not_import_the_sdks() -> None:
    code = (
        "import sys, spendgate.client\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in ('openai', 'anthropic', 'httpx')"
        " and not m.startswith('spendgate')))\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    listed = json.loads(out.stdout.strip().replace("'", '"'))
    assert not [m for m in listed if m.split(".")[0] in ("openai", "anthropic")]
