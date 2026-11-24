"""Steps 2 to 7 as pure functions: key extraction, validation, pricing, limits, the hold request."""

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import pytest

from spendgate.config import Settings, load
from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.limits import InMemoryLimitStore
from spendgate.core.policy import KeyPolicy, Limits
from spendgate.ledger.key_types import Principal
from spendgate.ledger.port import LedgerUnavailable
from spendgate.pipeline import steps
from spendgate.pipeline.auth import authenticate, presented_key
from spendgate.pipeline.model import CHAT, EMBEDDINGS, MESSAGES, TRANSCRIPTIONS, Endpoint, Inbound
from spendgate.pipeline.routing import Router
from spendgate.pipeline.steps import Context, Prepared
from spendgate.providers.anthropic import AnthropicProvider
from spendgate.providers.openai import OpenAIProvider
from spendgate.providers.port import CleanRequest, Json, Provider
from tests.integration.harness import ENV, NOW_MS, async_test
from tests.unit.audio_builders import wav

CONFIG = """\
server: {listen: "127.0.0.1:8080", key_pepper: "env:TEST_PEPPER"}
ledger: {currency: USD, backend: {kind: sqlite, path: ./l.db}}
providers:
  openai: {base_url: "https://openai.upstream.test", credential: "env:OPENAI_KEY"}
  anthropic: {base_url: "https://anthropic.upstream.test", credential: "env:ANTHROPIC_KEY"}
catalog: {paths: [./o.yaml]}
defaults: {max_embedding_items: 2, limits: {rpm: 5}}
models:
  alpha:
    catalog_id: openai/gpt-6-luna
    deployments:
      - {provider: openai, base_url: "https://a.upstream.test"}
      - {provider: openai, base_url: "https://b.upstream.test"}
  alpha-b: {catalog_id: openai/gpt-6-luna, deployments: [{provider: openai}]}
  alpha-twin: {catalog_id: openai/gpt-6-luna, deployments: [{provider: openai}]}
  embed: {catalog_id: openai/text-embedding-3-small, deployments: [{provider: openai}]}
  haiku: {catalog_id: anthropic/claude-haiku-5-5, deployments: [{provider: anthropic}]}
  whisper: {catalog_id: openai/whisper-test, deployments: [{provider: openai}]}
"""
OVERRIDE = """\
catalog_version: 1
models:
  - id: openai/whisper-test
    mode: transcription
    currency: USD
    effective_from: "2026-11-01"
    source_url: https://example.test/pricing
    verified: true
    meters: {audio_input_per_minute: "0.006"}
"""
KEY = KeyPolicy("key-1", "scope-1")
USER: list[Json] = [{"role": "user", "content": "hi"}]


def settings(tmp_path: Path) -> Settings:
    (tmp_path / "o.yaml").write_text(OVERRIDE)
    (tmp_path / "s.yaml").write_text(CONFIG)
    return load(tmp_path / "s.yaml", env=ENV)


def inbound(
    endpoint: Endpoint = CHAT,
    body: Mapping[str, Json] | None = None,
    headers: Mapping[str, str] | None = None,
    files: Mapping[str, bytes] | None = None,
) -> Inbound:
    return Inbound(endpoint, headers or {}, body or {}, files or {})


def run_prepare(
    s: Settings, ib: Inbound, key: KeyPolicy = KEY, request_id: str = "sg_req_abc"
) -> Prepared | Refusal:
    providers: dict[str, Provider] = {"openai": OpenAIProvider(), "anthropic": AnthropicProvider()}
    return steps.prepare(Context(s, providers, ib, key, request_id, NOW_MS))


def prepared(result: Prepared | Refusal) -> Prepared:
    assert isinstance(result, Prepared), result
    return result


def refusal(result: Prepared | Refusal) -> Refusal:
    assert isinstance(result, Refusal), result
    return result


# --- step 2 -------------------------------------------------------------------------------------


def test_presented_key_from_either_header_and_they_must_agree() -> None:
    assert presented_key({"authorization": "Bearer sg-x"}) == "sg-x"
    assert presented_key({"authorization": "bearer sg-x"}) == "sg-x"
    assert presented_key({"x-api-key": " sg-x "}) == "sg-x"
    assert presented_key({"authorization": "Bearer sg-x", "x-api-key": "sg-x"}) == "sg-x"
    for headers in ({}, {"authorization": "Basic abc"}, {"authorization": "Bearer "},
                    {"authorization": "Bearer a", "x-api-key": "b"}):  # fmt: skip
        assert refusal_of(presented_key(headers)).code is ErrorCode.UNAUTHORIZED


def refusal_of(value: str | Refusal) -> Refusal:
    assert isinstance(value, Refusal)
    return value


@async_test
async def test_authenticate_fails_closed_when_the_ledger_is_down() -> None:
    class Down:
        async def verify_api(self, raw: str, now: int) -> Principal | Refusal:
            raise LedgerUnavailable("gone")

        async def still_active(self, principal: Principal, now: int) -> bool:
            raise LedgerUnavailable("gone")

    result = await authenticate(Down(), {"authorization": "Bearer sg-x"}, 0)
    assert isinstance(result, Refusal) and result.code is ErrorCode.LEDGER_UNAVAILABLE


# --- routing and labels --------------------------------------------------------------------------


def test_find_route_by_alias_or_catalog_id_in_config_order(tmp_path: Path) -> None:
    s = settings(tmp_path)
    assert steps.find_route(s.routes, "alpha-b") == s.routes["alpha-b"]
    assert steps.find_route(s.routes, "openai/gpt-6-luna") == s.routes["alpha"]  # first alias
    assert steps.find_route(s.routes, "missing") is None


def test_labels_and_attribution_fallbacks() -> None:
    labels, user, tags = steps.labels_of(
        {"x-spendgate-session": "s1", "x-spendgate-tags": "a, b"}, {"user": "alice@example.test"}
    )
    assert (labels.session, labels.tags) == ("s1", ("a", "b"))
    assert user is None  # an email is not a valid attribution id: dropped, never refused
    assert tags == {"a": "1", "b": "1"}
    assert steps.labels_of({}, {"user": "u-1"})[1] == "u-1"
    assert steps.labels_of({}, {"metadata": {"user_id": "u-2"}})[1] == "u-2"
    assert steps.labels_of({"x-spendgate-user": "h"}, {"user": "u-1"})[1] == "h"


# --- steps 3 to 5 ---------------------------------------------------------------------------------


def test_missing_ceiling_is_injected_per_kind(tmp_path: Path) -> None:
    s = settings(tmp_path)
    chat = prepared(run_prepare(s, inbound(body={"model": "alpha", "messages": USER})))
    assert chat.request.body["max_completion_tokens"] == 128_000
    assert chat.request.output_ceiling == 128_000
    assert chat.request.endpoint == "/v1/chat/completions"
    messages = prepared(run_prepare(s, inbound(MESSAGES, {"model": "haiku", "messages": USER})))
    assert messages.request.body["max_tokens"] == messages.request.output_ceiling == 128_000


@pytest.mark.parametrize(
    "ceilings",
    [
        {},
        {"max_tokens": 7},
        {"max_completion_tokens": 7},
        {"max_tokens": 7, "max_completion_tokens": 900},
    ],
    ids=["none", "max_tokens", "max_completion_tokens", "both"],
)
def test_m15_the_clean_request_carries_one_ceiling_equal_to_the_held_one(
    tmp_path: Path, ceilings: dict[str, int]
) -> None:
    s = settings(tmp_path)
    done = prepared(run_prepare(s, inbound(body={"model": "alpha", "messages": USER, **ceilings})))
    held = done.request.output_ceiling
    assert held == min(ceilings.values(), default=128_000)
    fields = {k: v for k, v in done.request.body.items() if k.startswith("max_")}
    assert fields == {"max_completion_tokens": held}
    reference = prepared(
        run_prepare(s, inbound(body={"model": "alpha", "messages": USER, "max_tokens": held}))
    )
    assert done.pricing.worst == reference.pricing.worst  # the hold was sized from `held`
    assert done.tokens == reference.tokens


def test_key_ceiling_wins_when_smaller(tmp_path: Path) -> None:
    s = settings(tmp_path)
    key = KeyPolicy("k", "s", max_output_tokens=50)
    done = prepared(run_prepare(s, inbound(body={"model": "alpha", "messages": USER}), key))
    assert done.request.body["max_completion_tokens"] == 50
    over = refusal(
        run_prepare(s, inbound(body={"model": "alpha", "messages": USER, "max_tokens": 51}), key)
    )
    assert over.code is ErrorCode.REQUEST_TOO_LARGE and over.param == "max_tokens"


def test_prepared_facts(tmp_path: Path) -> None:
    s = settings(tmp_path)
    ib = inbound(
        body={"model": "alpha", "messages": USER, "max_tokens": 100},
        headers={"authorization": "Bearer sg-secret", "x-api-key": "sg-secret", "x-other": "1",
                 "x-spendgate-user": "u1"},
    )  # fmt: skip
    p = prepared(run_prepare(s, ib, request_id="sg_req_01abc"))
    assert "authorization" not in p.request.headers and "x-api-key" not in p.request.headers
    assert p.request.headers["x-other"] == "1"  # only credentials are withheld from adapters
    assert p.request.response_id == "chatcmpl-01abc" and p.request.created == NOW_MS // 1000
    assert p.user_label == "u1" and p.free_statuses == frozenset({429})
    assert p.pricing.worst.nano > 0 and p.pricing.worst.currency == "USD"
    assert p.limits == Limits(5, None, None)  # config defaults.limits.rpm fills the key's gaps
    assert steps._limits(Limits(9, 3, 2), s) == Limits(9, 3, 2)


def test_refusals_in_the_documented_order(tmp_path: Path) -> None:
    s = settings(tmp_path)
    cases: list[tuple[Inbound, KeyPolicy, ErrorCode, str | None]] = [
        (inbound(body={}), KEY, ErrorCode.INVALID_REQUEST, "model"),
        (inbound(body={"model": 3}), KEY, ErrorCode.INVALID_REQUEST, "model"),
        (inbound(body={"model": "x"}), KEY, ErrorCode.MODEL_NOT_ALLOWED, None),
        (inbound(body={"model": "alpha"}), KeyPolicy("k", "s", allowed_models=("haiku",)),
         ErrorCode.MODEL_NOT_ALLOWED, None),
        (inbound(body={"model": "embed", "stream": True, "input": "x"}, endpoint=EMBEDDINGS), KEY,
         ErrorCode.INVALID_REQUEST, "stream"),
        (inbound(body={"model": "alpha", "messages": USER, "max_tokens": 10**9}), KEY,
         ErrorCode.REQUEST_TOO_LARGE, "max_tokens"),
        (inbound(body={"model": "alpha", "messages": USER, "bogus": 1}), KEY,
         ErrorCode.INVALID_REQUEST, "bogus"),
        (inbound(body={"model": "alpha", "messages": USER, "max_tokens": 5},
                 headers={"x-spendgate-session": "s"}), KEY, ErrorCode.INVALID_REQUEST,
         "X-Spendgate-Session"),
        (inbound(EMBEDDINGS, {"model": "embed", "input": ["a", "b", "c"]}), KEY,
         ErrorCode.INVALID_REQUEST, "input"),
    ]  # fmt: skip
    for ib, key, code, param in cases:
        got = refusal(run_prepare(s, ib, key))
        assert (got.code, got.param) == (code, param), ib


def test_streaming_is_accepted_on_the_three_streaming_endpoints_only(tmp_path: Path) -> None:
    s = settings(tmp_path)
    body: dict[str, Json] = {"model": "alpha", "messages": USER, "max_tokens": 5, "stream": True}
    assert prepared(run_prepare(s, inbound(body=body))).request.stream is True
    quiet = prepared(run_prepare(s, inbound(body=body | {"stream": False})))
    assert quiet.request.stream is False
    got = refusal(
        run_prepare(s, inbound(EMBEDDINGS, {"model": "embed", "input": "x", "stream": True}))
    )
    assert (got.code, got.param) == (ErrorCode.INVALID_REQUEST, "stream")


def test_model_allowed_by_alias_or_by_catalog_id(tmp_path: Path) -> None:
    s = settings(tmp_path)
    body: dict[str, Json] = {"model": "alpha", "messages": USER, "max_tokens": 5}
    for allowed in (("alpha",), ("openai/*",), ("openai/gpt-6-luna",)):
        assert isinstance(
            run_prepare(s, inbound(body=body), KeyPolicy("k", "s", allowed_models=allowed)),
            Prepared,
        )


def test_unpriced_model_and_per_call_ceiling(tmp_path: Path) -> None:
    s = settings(tmp_path)
    body: dict[str, Json] = {"model": "alpha", "messages": USER, "max_tokens": 5}
    providers: dict[str, Provider] = {"openai": OpenAIProvider()}
    early = Context(s, providers, inbound(body=body), KEY, "sg_req_a", NOW_MS - 10 * 86_400_000)
    assert refusal(steps.prepare(early)).code is ErrorCode.MODEL_NOT_PRICED
    tight = KeyPolicy("k", "s", per_call_ceiling_nano=1)
    assert refusal(run_prepare(s, inbound(body=body), tight)).code is ErrorCode.REQUEST_TOO_LARGE


def test_transcription_is_estimated_from_the_measured_audio(tmp_path: Path) -> None:
    s = settings(tmp_path)
    ib = inbound(TRANSCRIPTIONS, {"model": "whisper"}, files={"file": wav(60.0)})
    p = prepared(run_prepare(s, ib))
    # 60 s at 0.006 USD per minute, rounded up to the nano-unit.
    assert p.pricing.worst.to_decimal() == Decimal("0.006")
    assert dict(p.request.files) == {"file": wav(60.0)}


# --- step 6 ---------------------------------------------------------------------------------------


@async_test
async def test_limits_admit_together_or_refuse_with_a_hint() -> None:
    store = InMemoryLimitStore()
    limits = Limits(rpm=1, tpm=100, max_concurrent=1)
    first = await steps.admit_limits(store, "k", limits, 10, 0)
    assert first is not None and not isinstance(first, Refusal)
    again = refusal_of_limit(await steps.admit_limits(store, "k", limits, 10, 1000))
    assert again.code is ErrorCode.RATE_LIMITED and again.details["limit"] in {"rpm", "concurrency"}
    assert again.retry_after_seconds is not None and again.retry_after_seconds >= 1
    huge = refusal_of_limit(await steps.admit_limits(store, "other", limits, 101, 0))
    assert huge.code is ErrorCode.REQUEST_TOO_LARGE and huge.details == {"limit": "tpm"}
    assert await steps.admit_limits(store, "none", Limits(None, None, None), 5, 0) is None


def refusal_of_limit(value: object) -> Refusal:
    assert isinstance(value, Refusal)
    return value


# --- step 7 ---------------------------------------------------------------------------------------


def test_hold_request_holds_the_worst_case_for_every_candidate(tmp_path: Path) -> None:
    s = settings(tmp_path)
    body: dict[str, Json] = {"model": "alpha", "messages": USER, "max_tokens": 100}
    p = prepared(run_prepare(s, inbound(body=body, headers={"x-spendgate-tags": "t1"})))
    targets = Router(s.routes, s.health).targets("alpha")
    req = steps.hold_request(s, p, targets, KEY, "/v1/chat/completions", "sg_req_a", "inst", NOW_MS)
    assert req.worst_nano == p.pricing.worst.nano and req.providers == ("openai",)
    assert [c.deployment for c in req.meta.candidates] == ["alpha/0", "alpha/1"]
    assert req.meta.catalog_sha == s.catalog_sha256 and dict(req.meta.labels) == {"t1": "1"}
    assert req.model == "openai/gpt-6-luna" and req.lease_ms == s.lease_ms and req.plan is None


def test_unexpected_provider_validate_error_is_a_400_refusal_not_a_500(tmp_path: Path) -> None:
    """FZ-01 defence in depth: nothing is held at this point, so any surprise refuses."""

    class Exploding(OpenAIProvider):
        def validate(self, request: CleanRequest) -> Refusal | None:
            raise TypeError("unhashable type: 'list'")

    s = settings(tmp_path)
    ib = inbound(body={"model": "alpha", "messages": USER, "max_tokens": 5})
    context = Context(s, {"openai": Exploding(), "anthropic": AnthropicProvider()}, ib, KEY,
                      "sg_req_abc", NOW_MS)  # fmt: skip
    got = refusal(steps.prepare(context))
    assert got.code is ErrorCode.INVALID_REQUEST
