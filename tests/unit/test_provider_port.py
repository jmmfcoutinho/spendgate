"""The provider port's shared types."""

import importlib
import typing

import pytest

from spendgate.core.errors import ErrorCode
from spendgate.core.outcomes import Outcome
from spendgate.providers.bedrock_stream import BedrockStream
from spendgate.providers.google_stream import GoogleRelay
from spendgate.providers.port import (
    AuthMode,
    CleanRequest,
    Deployment,
    Provider,
    SseLimits,
    StreamBroken,
    StreamLimitExceeded,
    StreamRelay,
    Untranslatable,
    UpstreamError,
    UpstreamRequest,
)
from spendgate.providers.sse import SseRelay


def test_reprs_hide_secrets_and_user_content() -> None:
    dep = Deployment(
        "openai", "https://api.example", "gpt-x", AuthMode.API_KEY, credential="sk-secret"
    )
    req = UpstreamRequest(
        "POST", "https://u", {"authorization": "Bearer sk-secret"}, b"private text"
    )
    clean = CleanRequest(
        "/v1/chat/completions",
        "m",
        {"messages": "private words"},
        headers={"anthropic-version": "v"},
        files={"file": b"private audio"},
    )
    text = repr(dep) + repr(req) + repr(clean)
    for hidden in ("sk-secret", "private text", "anthropic-version", "private words", "audio"):
        assert hidden not in text


@pytest.mark.parametrize(
    ("error", "free", "expected_free", "outcome"),
    [
        (UpstreamError(ErrorCode.UPSTREAM_THROTTLED, status=429), {429}, True, Outcome.THROTTLED),
        (
            UpstreamError(ErrorCode.UPSTREAM_THROTTLED, status=529),
            {429},
            False,
            Outcome.UPSTREAM_5XX,
        ),
        (
            UpstreamError(ErrorCode.UPSTREAM_THROTTLED, status=529),
            {429, 529},
            True,
            Outcome.THROTTLED,
        ),
        (UpstreamError(ErrorCode.INVALID_REQUEST, status=400), {429}, False, Outcome.CLIENT_ERROR),
        (
            UpstreamError(ErrorCode.UPSTREAM_UNAVAILABLE, sent=False),
            {429},
            True,
            Outcome.UNREACHABLE,
        ),
    ],
)
def test_not_processed_and_outcome_follow_core_rules(
    error: UpstreamError, free: set[int], expected_free: bool, outcome: Outcome
) -> None:
    assert error.not_processed(free) is expected_free
    assert error.outcome(free) is outcome


def test_sent_error_without_status_has_no_answered_outcome() -> None:
    with pytest.raises(ValueError, match="without a status"):
        UpstreamError(ErrorCode.UPSTREAM_UNAVAILABLE).outcome({429})


def test_sse_limits_are_frozen_positive_numbers() -> None:
    limits = SseLimits()
    assert 0 < limits.max_line_bytes <= limits.max_event_bytes <= limits.max_stream_bytes
    with pytest.raises(AttributeError):
        limits.max_events = 1  # type: ignore[misc]


def test_untranslatable_carries_an_invalid_request_refusal_naming_the_param() -> None:
    err = Untranslatable("n must be 1", "n")
    assert (err.refusal.code, err.refusal.message, err.refusal.param) == (
        ErrorCode.INVALID_REQUEST,
        "n must be 1",
        "n",
    )
    assert Untranslatable("endpoint refused").refusal.param is None


def test_a_crossed_stream_limit_is_a_broken_stream() -> None:
    assert issubclass(StreamLimitExceeded, StreamBroken)


SLUGS = ("openai", "azure_openai", "anthropic", "bedrock", "google", "azure_speech", "elevenlabs")


def _adapters() -> list[Provider]:
    """Every adapter class, found in its own module, so a new adapter can't be left out."""
    found: list[Provider] = []
    for slug in SLUGS:
        module = importlib.import_module(f"spendgate.providers.{slug}")
        classes = [
            obj
            for obj in vars(module).values()
            if isinstance(obj, type)
            and obj.__module__ == module.__name__
            and getattr(obj, "name", None) == slug
        ]
        assert len(classes) <= 1, f"{slug}: more than one adapter class"
        found.extend(cls() for cls in classes)
    return found


ADAPTERS = _adapters()


def test_every_provider_module_has_an_adapter() -> None:
    assert {a.name for a in ADAPTERS} == set(SLUGS)


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.name)
def test_an_adapter_exposes_exactly_the_port_and_no_other_public_hooks(adapter: Provider) -> None:
    port = typing.get_protocol_members(Provider)
    assert port == {
        "name",
        "validate",
        "prepare",
        "translate",
        "response",
        "relay",
        "usage",
        "error",
    }
    public = {n for n in dir(type(adapter)) if not n.startswith("_")}
    assert public == port, f"extra: {public - port}, missing: {port - public}"
    assert not {n for n in vars(adapter) if not n.startswith("_")}  # no public instance state


def test_the_adapter_name_is_its_module_name() -> None:
    for adapter in ADAPTERS:
        assert type(adapter).__module__ == f"spendgate.providers.{adapter.name}"


@pytest.mark.parametrize(
    "relay_type", [SseRelay, BedrockStream, GoogleRelay], ids=lambda t: t.__name__
)
def test_every_relay_has_the_stream_relay_methods(relay_type: type) -> None:
    members = typing.get_protocol_members(StreamRelay)
    assert members == {"feed", "close", "usage"}
    assert all(callable(getattr(relay_type, m)) for m in members)
