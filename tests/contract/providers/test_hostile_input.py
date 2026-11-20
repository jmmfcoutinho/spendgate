"""Adapters never raise on hostile answers (CR-01, CR-08): unreadable means None or a mapped error.

A raise from `usage()`, `error()` or `response()` after the request was sent would lose the
attempt and release the hold at cost 0, so every adapter gets the same hostile bodies and headers.
"""

import contextlib
import json
from collections.abc import Callable

import pytest

from spendgate.core.cost import Usage
from spendgate.providers.anthropic import AnthropicProvider
from spendgate.providers.azure_openai import AzureOpenAIProvider
from spendgate.providers.azure_speech import AzureSpeech
from spendgate.providers.bedrock import Bedrock
from spendgate.providers.elevenlabs import ElevenLabs
from spendgate.providers.google import GoogleProvider
from spendgate.providers.openai import OpenAIProvider
from spendgate.providers.port import (
    INT64_MAX,
    CleanRequest,
    Provider,
    SseEvent,
    StreamBroken,
    UpstreamResponse,
)
from spendgate.providers.sse import serialise

DEEP = b"[" * 200_000  # json.loads raises RecursionError, not ValueError
DEEP_OBJ = b'{"a":' * 100_000
HUGE = 2**80
NASTY_HEADERS = [
    {"retry-after": "²"},
    {"Retry-After": "9" * 5000},
    {"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"},
    {"retry-after-ms": "9" * 5000},
    {"retry-after-ms": "²"},
    {"x-amzn-errortype": "²:", "retry-after": "²"},
    {"x-spendgate-cost": "9" * 5000},
]

PROVIDERS: dict[str, Callable[[], Provider]] = {
    "openai": OpenAIProvider,
    "azure_openai": AzureOpenAIProvider,
    "anthropic": AnthropicProvider,
    "google": GoogleProvider,
    "bedrock": Bedrock,
    "azure_speech": AzureSpeech,
    "elevenlabs": ElevenLabs,
}
ENDPOINTS = {
    "azure_speech": "/v1/audio/transcriptions",
    "elevenlabs": "/v1/audio/speech",
    "anthropic": "/v1/messages",
}
# A usage figure each adapter reads, with a counter that does not fit int64.
HUGE_USAGE = {
    "openai": {"usage": {"prompt_tokens": HUGE, "completion_tokens": 1}},
    "azure_openai": {"usage": {"prompt_tokens": HUGE, "completion_tokens": 1}},
    "anthropic": {"usage": {"input_tokens": HUGE, "output_tokens": 1}},
    "google": {"usageMetadata": {"promptTokenCount": HUGE}},
    "bedrock": {"usage": {"inputTokens": HUGE, "outputTokens": 1}},
}
# Counters that each fit int64 but whose normalised sum does not.
SUM_OVERFLOW = {
    "anthropic": {
        "usage": {"input_tokens": INT64_MAX, "output_tokens": 1, "cache_read_input_tokens": 5}
    },
    "bedrock": {"usage": {"inputTokens": INT64_MAX, "outputTokens": 1, "cacheReadInputTokens": 5}},
    "google": {"usageMetadata": {"promptTokenCount": INT64_MAX, "toolUsePromptTokenCount": 5}},
}


def request(name: str) -> CleanRequest:
    body: dict[str, object] = {"input": "hello", "model": "m", "voice": "v"}
    files = {"file": b"RIFF\x00\x00\x00\x00WAVE"} if name == "azure_speech" else {}
    return CleanRequest(
        ENDPOINTS.get(name, "/v1/chat/completions"),
        "gw",
        body,  # type: ignore[arg-type]
        files=files,
        response_id="id",
        created=1,
    )


def hostile_bodies() -> list[bytes]:
    return [
        DEEP,
        DEEP_OBJ,
        b'{"usage":' + b"[" * 100_000,
        b"\xff\xfe\x00",
        b'{"usage": {"prompt_tokens": 1e999}}',
        b'{"error": {"message": 5, "code": [1]}, "detail": 7, "usageMetadata": 3}',
        b"null",
        b'"\xc3\x28"',
    ]


@pytest.mark.parametrize("name", PROVIDERS)
@pytest.mark.parametrize("status", [200, 429, 503, 400])
def test_t_adp_01_usage_error_response_never_raise(name: str, status: int) -> None:
    provider, req = PROVIDERS[name](), request(name)
    for body in hostile_bodies():
        for headers in [*NASTY_HEADERS, {}]:
            resp = UpstreamResponse(status, headers, body)
            assert provider.usage(req, resp) is None or isinstance(provider.usage(req, resp), Usage)
            mapped = provider.error(resp)
            assert mapped.retry_after_seconds is None or 0 <= mapped.retry_after_seconds <= 86_400
            assert isinstance(provider.response(req, resp).status, int)


@pytest.mark.parametrize("name", list(HUGE_USAGE))
def test_t_adp_02_usage_counter_beyond_int64_is_unreadable(name: str) -> None:
    resp = UpstreamResponse(200, {}, json.dumps(HUGE_USAGE[name]).encode())
    assert PROVIDERS[name]().usage(request(name), resp) is None


@pytest.mark.parametrize("name", list(SUM_OVERFLOW))
def test_t_adp_03_normalised_sum_beyond_int64_is_unreadable(name: str) -> None:
    resp = UpstreamResponse(200, {}, json.dumps(SUM_OVERFLOW[name]).encode())
    assert PROVIDERS[name]().usage(request(name), resp) is None


def test_t_adp_04_a_throttle_with_a_nasty_retry_after_still_maps() -> None:
    """The CR-01 probe: a Google 503 with a 5000-digit `Retry-After` is an error, not a crash."""
    resp = UpstreamResponse(503, {"Retry-After": "9" * 5000}, b"{}")
    for make in PROVIDERS.values():
        mapped = make().error(resp)
        assert mapped.status == 503
        assert mapped.sent
    assert GoogleProvider().error(UpstreamResponse(503, {"retry-after": "²"}, b"{}")).sent


class _Exploding:
    """Stands in for an adapter internal that raises, to prove the decorators catch it."""

    def __getattr__(self, name: str) -> object:
        raise RecursionError(name)


def test_t_adp_05_an_internal_raise_is_unreadable_not_propagated() -> None:
    provider = OpenAIProvider()
    # `usage` on a response whose body property itself blows up
    resp = UpstreamResponse(200, {}, _Exploding())  # type: ignore[arg-type]
    assert provider.usage(request("openai"), resp) is None
    assert provider.error(resp).status == 200
    assert GoogleProvider().response(request("google"), resp).status == 502


def _stream_events(name: str) -> list[bytes]:
    names = {
        "anthropic": ["message_start", "message_delta"],
        "bedrock": ["metadata"],
    }.get(name, [None])
    bodies = [DEEP, DEEP_OBJ, json.dumps({"usage": {"input_tokens": HUGE}}).encode()]
    out = []
    for event in names:
        for body in bodies:
            out.append(serialise(SseEvent(event, body.decode("latin-1"))))
    return out


@pytest.mark.parametrize("name", ["openai", "azure_openai", "anthropic", "google"])
def test_t_adp_06_sse_relays_survive_hostile_events(name: str) -> None:
    provider, req = PROVIDERS[name](), request(name)
    relay = provider.relay(req)
    for chunk in _stream_events(name):
        with contextlib.suppress(
            StreamBroken
        ):  # refusing to carry it is fine; any other raise is the bug
            relay.feed(chunk)
    with contextlib.suppress(StreamBroken):
        relay.close()
    assert relay.usage() is None
