"""Anthropic adapter contract: allowlist, headers, usage, cost, errors, no secrets in reprs."""

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import httpx
import pytest
import yaml

from spendgate.core.catalog import parse
from spendgate.core.cost import Usage, actual_cost
from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.outcomes import ANTHROPIC_FREE_STATUSES, Outcome
from spendgate.providers.anthropic import (
    CURRENT_VERSION,
    AnthropicConfig,
    AnthropicProvider,
)
from spendgate.providers.port import (
    CleanRequest,
    Deployment,
    Json,
    SseEvent,
    SseLimits,
    StreamBroken,
    StreamLimitExceeded,
    StreamRelay,
    UpstreamRequest,
    UpstreamResponse,
)
from spendgate.providers.sse import serialise
from tests.contract.providers.ceiling import ceiling_of
from tests.fakes import BASE_URL, FakeUpstream, FakeUsage, Script

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "sk-ant-test-secret-do-not-leak"  # noqa: S105  # pragma: allowlist secret
DEPLOYMENT = Deployment("anthropic", BASE_URL, "claude-opus-5-5", credential=SECRET)
PROVIDER = AnthropicProvider()


def load(name: str) -> dict[str, Json]:
    return cast("dict[str, Json]", json.loads((FIXTURES / name).read_text()))


SDK = load("sdk_requests_20261117.json")


def sdk(name: str) -> tuple[dict[str, Json], dict[str, str]]:
    case = cast("dict[str, Json]", SDK[name])
    return (
        cast("dict[str, Json]", case["body"]),
        cast("dict[str, str]", case["headers"]),
    )


def request(body: Mapping[str, Json], headers: Mapping[str, str] | None = None) -> CleanRequest:
    return CleanRequest(
        "/v1/messages", str(body.get("model", "")), body, body.get("stream") is True, headers or {},
        output_ceiling=ceiling_of(body),
    )  # fmt: skip


def clean(
    body: Mapping[str, Json],
    headers: Mapping[str, str] | None = None,
    provider: AnthropicProvider = PROVIDER,
) -> CleanRequest:
    req = request(body, headers)
    assert provider.validate(req) is None
    return req


def refusal(
    body: Mapping[str, Json],
    headers: Mapping[str, str] | None = None,
    provider: AnthropicProvider = PROVIDER,
) -> Refusal:
    result = provider.validate(request(body, headers))
    assert result is not None, "expected a refusal"
    assert result.code is ErrorCode.INVALID_REQUEST
    return result


def relay_of(events: list[SseEvent], req: CleanRequest | None = None) -> StreamRelay:
    relay = PROVIDER.relay(req or request({**MIN, "stream": True}))
    for event in events:
        relay.feed(serialise(event))
    return relay


MIN: dict[str, Json] = {
    "model": "m",
    "max_tokens": 5,
    "messages": [{"role": "user", "content": "hi"}],
}


REQ = request(MIN)


def sse_events(raw: bytes) -> list[SseEvent]:
    events = []
    for chunk in raw.decode().split("\n\n"):
        lines = [ln for ln in chunk.splitlines() if ln and not ln.startswith(":")]
        name = next((ln[7:].strip() for ln in lines if ln.startswith("event:")), None)
        data = "\n".join(ln[5:].strip() for ln in lines if ln.startswith("data:"))
        if data:
            events.append(SseEvent(name, data))
    return events


async def send(fake: FakeUpstream, req: UpstreamRequest) -> httpx.Response:
    async with fake.client() as client:
        return await client.request(
            req.method, req.url, headers=dict(req.headers), content=req.body
        )


# --- the official SDK's request shapes are accepted unmodified ------------------------------


@pytest.mark.parametrize(
    "name",
    ["plain", "stream", "system_blocks_and_cache", "tools_roundtrip", "thinking_and_image"],
)
def test_sdk_request_shapes_are_accepted(name: str) -> None:
    body, headers = sdk(name)
    req = clean(body, headers)
    assert req.stream is (body.get("stream") is True)
    up = json.loads(PROVIDER.translate(req, DEPLOYMENT).body)
    assert up == {**body, "model": DEPLOYMENT.model}  # nothing the SDK sent is dropped


def test_sdk_sampling_fields_are_accepted_only_on_the_old_version() -> None:
    body, headers = sdk("legacy_sampling_old_version")
    old = AnthropicProvider(
        AnthropicConfig(allowed_versions=("2023-06-01",), default_version="2023-06-01")
    )
    assert old.validate(request(body, headers)) is None
    # On the current version Anthropic itself answers 400 to these, so refuse before sending.
    r = refusal(body, {"anthropic-version": CURRENT_VERSION})
    assert r.param == "temperature"


def test_header_names_match_in_any_case() -> None:
    req = clean(MIN, {"Anthropic-Version": CURRENT_VERSION})
    assert PROVIDER.translate(req, DEPLOYMENT).headers["anthropic-version"] == CURRENT_VERSION


# --- allowlist: unknown and refused fields name the parameter -------------------------------


def test_unknown_top_level_field_is_refused_by_name() -> None:
    assert refusal({**MIN, "frobnicate": 1}).param == "frobnicate"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("inference_geo", "us"),  # 1.1x on every token category
        ("speed", "fast"),  # fast mode premium
        ("service_tier", "priority"),
        ("container", {"id": "c1"}),
        ("mcp_servers", []),
        ("context_management", {}),
        ("cache_control", {"type": "ephemeral"}),
    ],
)
def test_price_changing_options_the_catalog_does_not_price_are_refused(
    field: str, value: Json
) -> None:
    r = refusal({**MIN, field: value})
    assert r.param == field


def test_server_tools_are_refused_only_custom_tools_pass() -> None:
    tool: Json = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
    assert refusal({**MIN, "tools": [tool]}).param == "tools[0].max_uses"
    assert refusal({**MIN, "tools": [{"type": "code_execution_20250522", "name": "x"}]}).param == (
        "tools[0].type"
    )


@pytest.mark.parametrize(
    ("source", "param"),
    [
        ({"type": "url", "url": "https://x"}, "messages[0].content[0].source.url"),
        ({"type": "file", "file_id": "f"}, "messages[0].content[0].source.file_id"),
    ],
)
def test_url_and_file_image_sources_are_refused(source: Json, param: str) -> None:
    content: Json = [{"type": "image", "source": source}]
    r = refusal({**MIN, "messages": [{"role": "user", "content": content}]})
    assert r.param == param


def test_nested_unknown_fields_name_their_path() -> None:
    msgs: Json = [{"role": "user", "content": [{"type": "text", "text": "a", "bogus": 1}]}]
    assert refusal({**MIN, "messages": msgs}).param == "messages[0].content[0].bogus"
    assert refusal({**MIN, "metadata": {"user_id": "u", "x": 1}}).param == "metadata.x"
    assert refusal({**MIN, "thinking": {"type": "enabled", "z": 1}}).param == "thinking.z"


def test_unsupported_block_type_is_refused() -> None:
    msgs: Json = [{"role": "user", "content": [{"type": "document", "source": {}}]}]
    assert refusal({**MIN, "messages": msgs}).param == "messages[0].content[0].type"


def test_required_fields_and_types_are_checked() -> None:
    assert refusal({"model": "m", "messages": MIN["messages"]}).param == "max_tokens"
    assert refusal({**MIN, "max_tokens": True}).param == "max_tokens"
    assert refusal({**MIN, "stream": "yes"}).param == "stream"
    assert refusal({**MIN, "messages": []}).param == "messages"


def test_validate_is_pure_and_leaves_the_request_untouched() -> None:
    req = request(MIN, {"anthropic-version": CURRENT_VERSION})
    before = (dict(req.body), dict(req.headers))
    assert PROVIDER.validate(req) is None
    assert (dict(req.body), dict(req.headers)) == before


def test_other_endpoints_are_refused_naming_the_endpoint() -> None:
    other = CleanRequest("/v1/chat/completions", "m", MIN)
    result = PROVIDER.validate(other)
    assert result is not None
    assert result.param == "endpoint"


# --- version and beta headers ---------------------------------------------------------------


def test_version_defaults_to_the_gateway_default_when_absent() -> None:
    assert (
        PROVIDER.translate(clean(MIN), DEPLOYMENT).headers["anthropic-version"] == CURRENT_VERSION
    )


def test_listed_client_version_is_passed_through() -> None:
    config = AnthropicConfig(allowed_versions=("2023-06-01", CURRENT_VERSION))
    provider = AnthropicProvider(config)
    result = clean(MIN, {"anthropic-version": "2023-06-01"}, provider)
    upstream = provider.translate(result, DEPLOYMENT)
    assert upstream.headers["anthropic-version"] == "2023-06-01"


def test_unlisted_version_is_refused_naming_the_header() -> None:
    r = refusal(MIN, {"anthropic-version": "2024-01-01"})  # not on the default list
    assert r.param == "anthropic-version"


def test_default_version_must_be_allowed() -> None:
    with pytest.raises(ValueError, match="default_version"):
        AnthropicConfig(allowed_versions=("2023-06-01",))


def test_beta_values_must_be_on_the_allowlist() -> None:
    assert refusal(MIN, {"anthropic-beta": "fast-mode-2026-02-01"}).param == "anthropic-beta"
    config = AnthropicConfig(allowed_betas=frozenset({"b1", "b2"}))
    provider = AnthropicProvider(config)
    ok = clean(MIN, {"anthropic-beta": "b1, b2"}, provider)
    assert provider.translate(ok, DEPLOYMENT).headers["anthropic-beta"] == "b1,b2"
    assert refusal(MIN, {"anthropic-beta": "b1,b3"}, provider).param == "anthropic-beta"


def test_no_beta_header_is_sent_when_the_client_sent_none() -> None:
    assert "anthropic-beta" not in PROVIDER.translate(clean(MIN), DEPLOYMENT).headers


# --- translate: auth, URL, model ------------------------------------------------------------


def test_translate_uses_config_base_fixed_path_and_the_deployment_credential() -> None:
    dep = Deployment("anthropic", "https://api.example.test/", "real-model", credential=SECRET)
    up = PROVIDER.translate(clean({**MIN, "model": "gateway-alias"}), dep)
    assert up.url == "https://api.example.test/v1/messages"
    assert up.headers["x-api-key"] == SECRET
    assert json.loads(up.body)["model"] == "real-model"
    assert up.method == "POST"


def test_translate_fails_closed_without_a_credential() -> None:
    with pytest.raises(ValueError, match="credential"):
        PROVIDER.translate(clean(MIN), Deployment("anthropic", BASE_URL, "m"))


def test_translate_refuses_a_deployment_of_another_provider() -> None:
    with pytest.raises(ValueError, match="Anthropic"):
        PROVIDER.translate(clean(MIN), Deployment("openai", BASE_URL, "m", credential=SECRET))


# --- non-streamed and streamed against the fake ---------------------------------------------

CACHED = FakeUsage(
    input_tokens=100, output_tokens=200, cache_write_tokens=1000, cached_input_tokens=5000
)
EXPECTED = Usage(
    input_tokens=6100, output_tokens=200, cached_input_tokens=5000, cache_write_tokens=1000
)


async def test_non_stream_usage_adds_cache_tokens_back_into_input() -> None:
    fake = FakeUpstream(Script(usage=CACHED))
    resp = await send(fake, PROVIDER.translate(clean(MIN), DEPLOYMENT))
    got = PROVIDER.usage(REQ, UpstreamResponse(resp.status_code, dict(resp.headers), resp.content))
    assert got == EXPECTED
    recorded = fake.requests[0]
    assert recorded.headers["x-api-key"] == SECRET
    assert recorded.path == "/v1/messages"
    assert recorded.json()["model"] == "claude-opus-5-5"


async def test_stream_usage_takes_input_from_start_and_output_from_the_last_delta() -> None:
    fake = FakeUpstream(Script(usage=CACHED))
    req = clean({**MIN, "stream": True})
    up = PROVIDER.translate(req, DEPLOYMENT)
    assert up.stream
    resp = await send(fake, up)
    relay = PROVIDER.relay(req)
    out = relay.feed(resp.content) + relay.close()
    assert relay.usage() == EXPECTED
    assert sse_events(out) == sse_events(resp.content)  # relayed unchanged


async def test_stream_without_final_usage_is_none() -> None:
    fake = FakeUpstream(Script(usage=CACHED, final_usage=False))
    req = clean({**MIN, "stream": True})
    resp = await send(fake, PROVIDER.translate(req, DEPLOYMENT))
    relay = PROVIDER.relay(req)
    relay.feed(resp.content)
    assert relay.usage() is None


def _fixture_events(*, skip: str | None = None) -> list[SseEvent]:
    events = load("stream_events_20261117.json")["events"]
    assert isinstance(events, list)
    items = [i for i in events if isinstance(i, dict) and i["event"] != skip]
    return [SseEvent(str(i["event"]), json.dumps(i["data"])) for i in items]


def test_stream_with_only_message_start_is_none() -> None:
    assert relay_of(_fixture_events(skip="message_delta")).usage() is None


def test_stream_fixture_last_cumulative_delta_wins() -> None:
    assert relay_of(_fixture_events()).usage() == EXPECTED


def test_stream_with_garbled_usage_charges_the_hold() -> None:
    relay = relay_of(
        [
            SseEvent("message_start", "not json"),
            SseEvent("message_delta", json.dumps({"usage": {"output_tokens": 3}})),
        ]
    )
    assert relay.usage() is None


def test_relay_survives_any_split_of_the_upstream_bytes() -> None:
    raw = b"".join(serialise(e) for e in _fixture_events())
    relay = PROVIDER.relay(request({**MIN, "stream": True}))
    out = b"".join(relay.feed(raw[i : i + 7]) for i in range(0, len(raw), 7))
    assert out == raw
    assert relay.close() == b""
    assert relay.usage() == EXPECTED


def test_relay_enforces_the_stream_limits_and_rejects_a_half_event() -> None:
    req = request({**MIN, "stream": True})
    tiny = SseLimits(max_line_bytes=16, max_event_bytes=64, max_events=2, max_stream_bytes=256)
    with pytest.raises(StreamLimitExceeded):
        PROVIDER.relay(req, tiny).feed(b"data: " + b"x" * 17 + b"\n\n")
    with pytest.raises(StreamLimitExceeded):
        PROVIDER.relay(req, tiny).feed(b"data: a\n\ndata: b\n\ndata: c\n\n")
    with pytest.raises(StreamLimitExceeded):
        PROVIDER.relay(req, SseLimits(max_stream_bytes=8)).feed(b"data: 123456789\n\n")
    half = PROVIDER.relay(req)
    half.feed(b"event: message_start\ndata: {")
    with pytest.raises(StreamBroken):
        half.close()


def test_response_is_the_upstream_answer_unchanged() -> None:
    resp = UpstreamResponse(200, {"content-type": "application/json"}, b'{"id": "x"}')
    assert PROVIDER.response(request(MIN), resp) is resp


def test_recorded_response_fixture_ignores_the_ttl_split_and_extra_fields() -> None:
    body = json.dumps(load("message_response_20261117.json")).encode()
    assert PROVIDER.usage(REQ, UpstreamResponse(200, {}, body)) == EXPECTED


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"id": "x"}',
        b'{"usage": {"input_tokens": 1}}',
        b'{"usage": {"input_tokens": 1, "output_tokens": -2}}',
        b'{"usage": {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": "9"}}',
        b'{"usage": {"input_tokens": true, "output_tokens": 2}}',
    ],
)
def test_missing_or_garbled_usage_is_none_so_the_hold_is_charged(body: bytes) -> None:
    assert PROVIDER.usage(REQ, UpstreamResponse(200, {}, body)) is None


def test_cache_fields_default_to_zero_when_absent() -> None:
    body = b'{"usage": {"input_tokens": 7, "output_tokens": 2}}'
    assert PROVIDER.usage(REQ, UpstreamResponse(200, {}, body)) == Usage(
        input_tokens=7, output_tokens=2
    )


# --- cost: cache write plus cache read matches the catalog ----------------------------------

CATALOG_YAML = """
catalog_version: 1
models:
  - id: anthropic/claude-opus-5-5
    mode: messages
    currency: USD
    effective_from: "2026-01-01"
    source_url: https://platform.claude.com/docs/en/about-claude/pricing
    verified: false
    meters:
      input_per_mtok: "8"
      cache_write_per_mtok: "16"
      cached_input_per_mtok: "0.2"
      output_per_mtok: "40"
    limits: {max_output_tokens: 64000}
"""


def test_cache_write_and_read_cost_matches_the_catalog_prices() -> None:
    catalog = parse([yaml.safe_load(CATALOG_YAML)], "00" * 32, ledger_currency="USD")
    entry = catalog.lookup("anthropic", "claude-opus-5-5", 1_794_000_000_000)
    body = json.dumps(load("message_response_20261117.json")).encode()
    usage = PROVIDER.usage(REQ, UpstreamResponse(200, {}, body))
    assert usage is not None
    # 100 fresh in x 8 + 1000 written x 16 + 5000 read x 0.2 + 200 out x 40, per million.
    assert catalog.in_ledger(actual_cost(entry, usage)).nano == 25_800_000


# --- errors ---------------------------------------------------------------------------------


def err(status: int, headers: Mapping[str, str] | None = None) -> UpstreamResponse:
    body = b'{"type":"error","error":{"type":"x","message":"provider text"}}'
    return UpstreamResponse(status, headers or {}, body)


@pytest.mark.parametrize("status", [429, 529])
def test_429_and_529_are_throttled_and_free_with_retry_after(status: int) -> None:
    e = PROVIDER.error(err(status, {"Retry-After": "17"}))
    assert e.code is ErrorCode.UPSTREAM_THROTTLED
    assert e.retry_after_seconds == 17
    assert e.outcome(ANTHROPIC_FREE_STATUSES) is Outcome.THROTTLED
    assert e.not_processed(ANTHROPIC_FREE_STATUSES)
    assert e.message == "provider text"


def test_retry_after_in_http_date_form_is_ignored() -> None:
    e = PROVIDER.error(err(429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}))
    assert e.retry_after_seconds is None


@pytest.mark.parametrize("status", [400, 413, 422])
def test_4xx_is_a_client_error_settled_free_without_failover(status: int) -> None:
    e = PROVIDER.error(err(status))
    assert e.code is ErrorCode.INVALID_REQUEST
    assert e.outcome(ANTHROPIC_FREE_STATUSES) is Outcome.CLIENT_ERROR
    assert not e.not_processed(ANTHROPIC_FREE_STATUSES)


@pytest.mark.parametrize("status", [401, 402, 403, 404])
def test_our_key_billing_or_model_errors_are_upstream_unavailable(status: int) -> None:
    # protocol 4.2 rule 1: the provider's quota or billing error is not the caller's fault.
    e = PROVIDER.error(err(status))
    assert e.code is ErrorCode.UPSTREAM_UNAVAILABLE
    assert e.outcome(ANTHROPIC_FREE_STATUSES) is Outcome.CLIENT_ERROR


@pytest.mark.parametrize(
    ("status", "kind", "fault"),
    [
        (401, "authentication_error", True),
        (404, "not_found_error", True),  # the configured model: the body names nothing else
        (403, "permission_error", False),  # may answer one request's feature, not the deployment
        (400, "invalid_request_error", False),
        (402, "billing_error", False),
    ],
)
def test_only_a_bad_key_or_missing_model_is_a_deployment_fault(
    status: int, kind: str, fault: bool
) -> None:
    body = json.dumps({"type": "error", "error": {"type": kind, "message": "provider text"}})
    e = PROVIDER.error(UpstreamResponse(status, {}, body.encode()))
    assert e.deployment_fault is fault  # ADR-0033: what the shared breaker counts
    assert e.message == "provider text"
    assert not PROVIDER.error(err(403)).deployment_fault  # an unknown type is not a fault


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_is_upstream_5xx_and_charged(status: int) -> None:
    e = PROVIDER.error(err(status))
    assert e.code is ErrorCode.UPSTREAM_UNAVAILABLE
    assert e.outcome(ANTHROPIC_FREE_STATUSES) is Outcome.UPSTREAM_5XX


async def test_fake_error_bodies_map_through_the_adapter() -> None:
    fake = FakeUpstream(Script(status=529, retry_after="3"))
    resp = await send(fake, PROVIDER.translate(clean(MIN), DEPLOYMENT))
    e = PROVIDER.error(UpstreamResponse(resp.status_code, dict(resp.headers), resp.content))
    assert (e.code, e.retry_after_seconds) == (ErrorCode.UPSTREAM_THROTTLED, 3)


def test_unreadable_error_body_gives_an_empty_message() -> None:
    assert PROVIDER.error(UpstreamResponse(500, {}, b"<html>")).message == ""


# --- secrets --------------------------------------------------------------------------------


def test_no_secret_in_reprs_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        up = PROVIDER.translate(clean(MIN), DEPLOYMENT)
    assert SECRET not in repr(up)
    assert SECRET not in repr(DEPLOYMENT)
    assert SECRET not in repr(PROVIDER)
    assert SECRET not in caplog.text
    assert SECRET not in repr(UpstreamResponse(200, {"x-api-key": SECRET}, SECRET.encode()))


def test_older_sdk_version_is_accepted_by_default() -> None:
    # Older SDKs send 2023-06-01; the API version changes no price.
    assert "2023-06-01" in AnthropicConfig().allowed_versions
    assert AnthropicConfig().default_version == CURRENT_VERSION
