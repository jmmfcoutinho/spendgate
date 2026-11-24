"""Target 6: provider usage and error readers (CR-01 / CR-04 regressions).

Under test, through each adapter's public methods (`usage`, `error`, `response`, `relay`) and the
shared readers: OpenAI and Azure OpenAI (`openai_usage`), Anthropic (`_Collector`), Bedrock
(`converse_usage`, `BedrockStream`), Google (`read_usage`, `ChatStream`), ElevenLabs and Azure
Speech.

Inputs: JSON of the right shape with hostile values (huge integers past int64, negatives, booleans,
floats, NaN / Infinity, strings, nested nonsense), deeply nested and truncated JSON, invalid UTF-8,
SSE and event-stream wire bytes carrying such events, and Retry-After strings in every spelling.

Invariants: `usage` returns None or a `Usage` whose counters are non-negative and fit int64; `error`
returns an `UpstreamError` and a retry hint within [0, 86400] or None; `response` returns an
`UpstreamResponse`; none of them ever raises; a relay raises only `StreamBroken` (and its
subclasses) and its `usage()` is None or valid afterwards.
"""

import itertools
import json
import zlib
from decimal import Decimal
from typing import Any

from hypothesis import strategies as st

from spendgate.core.cost import Usage
from spendgate.providers import openai_shapes as shapes
from spendgate.providers.anthropic import PATH as ANTHROPIC_PATH
from spendgate.providers.anthropic import AnthropicProvider
from spendgate.providers.azure_openai import AzureOpenAIProvider
from spendgate.providers.azure_speech import AzureSpeech
from spendgate.providers.bedrock import Bedrock
from spendgate.providers.bedrock_stream import converse_usage
from spendgate.providers.elevenlabs import ElevenLabs
from spendgate.providers.google import GoogleProvider
from spendgate.providers.openai import OpenAIProvider
from spendgate.providers.openai_usage import parse_usage
from spendgate.providers.port import (
    INT64_MAX,
    CleanRequest,
    Provider,
    SseLimits,
    StreamBroken,
    UpstreamError,
    UpstreamResponse,
    retry_after_ms_seconds,
    retry_after_seconds,
)
from tests.fuzz.conftest import Case, run_fuzz
from tests.fuzz.test_bedrock_stream import frame, header
from tests.unit import audio_builders as fx

PROVIDERS: dict[str, Provider] = {
    "openai": OpenAIProvider(),
    "azure_openai": AzureOpenAIProvider(),
    "anthropic": AnthropicProvider(),
    "bedrock": Bedrock(),
    "google": GoogleProvider(),
    "elevenlabs": ElevenLabs(),
    "azure_speech": AzureSpeech(),
}
_WAV = fx.wav(1.0)

# --- hostile JSON of usage shape ---------------------------------------------------------------

_NUMBERS = st.one_of(
    st.integers(0, 5_000_000),
    st.sampled_from([0, 1, INT64_MAX, INT64_MAX + 1, 2**64, 2**200, -1, -(2**63), 10**30]),
    st.integers(-(2**70), 2**70),
    st.floats(),
    st.booleans(),
    st.none(),
    st.text(max_size=8),
    st.sampled_from(["12", "1e9", "NaN", "-0", ""]),
)
_KEYS = [
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "audio_tokens",
    "reasoning_tokens",
    "cache_write_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "prompt_tokens_details",
    "completion_tokens_details",
    "input_tokens_details",
    "output_tokens_details",
    "input_token_details",
    "type",
    "seconds",
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheWriteInputTokens",
    "promptTokenCount",
    "candidatesTokenCount",
    "cachedContentTokenCount",
    "thoughtsTokenCount",
    "toolUsePromptTokenCount",
    "durationMilliseconds",
]


def _usage_obj(depth: int = 2) -> st.SearchStrategy[Any]:
    leaf = _NUMBERS
    if depth == 0:
        return st.dictionaries(st.sampled_from(_KEYS), leaf, max_size=6)
    nested = _usage_obj(depth - 1)
    return st.dictionaries(
        st.sampled_from(_KEYS),
        leaf | nested | st.just("duration") | st.lists(leaf, max_size=2),
        max_size=8,
    )


def _deep(depth: int) -> Any:  # noqa: ANN401
    return json.loads("[" * depth + "]" * depth)


_N = st.one_of(
    st.integers(0, 100), st.integers(0, 100), st.integers(0, 100), _NUMBERS
)  # mostly plausible


def _shaped() -> st.SearchStrategy[Any]:
    """Documents in the shapes the adapters read, with a few hostile numbers among sane ones."""
    return st.one_of(
        st.builds(
            lambda a, b, c, d, e, f: {
                "usage": {
                    "prompt_tokens": a,
                    "completion_tokens": b,
                    "prompt_tokens_details": {"cached_tokens": c, "audio_tokens": d},
                    "completion_tokens_details": {"reasoning_tokens": e, "audio_tokens": f},
                }
            },
            _N,
            _N,
            _N,
            _N,
            _N,
            _N,
        ),
        st.builds(
            lambda a, b, c: {
                "usage": {
                    "input_tokens": a,
                    "output_tokens": b,
                    "input_tokens_details": {"cached_tokens": c},
                }
            },
            _N,
            _N,
            _N,
        ),
        st.builds(lambda a: {"usage": {"type": "duration", "seconds": a}}, _N),
        st.builds(
            lambda a, b, c, d: {
                "usage": {
                    "input_tokens": a,
                    "output_tokens": b,
                    "cache_creation_input_tokens": c,
                    "cache_read_input_tokens": d,
                }
            },
            _N,
            _N,
            _N,
            _N,
        ),
        st.builds(
            lambda a, b, c, d: {
                "usage": {
                    "inputTokens": a,
                    "outputTokens": b,
                    "cacheReadInputTokens": c,
                    "cacheWriteInputTokens": d,
                }
            },
            _N,
            _N,
            _N,
            _N,
        ),
        st.builds(
            lambda a, b, c, d, e: {
                "usageMetadata": {
                    "promptTokenCount": a,
                    "candidatesTokenCount": b,
                    "cachedContentTokenCount": c,
                    "thoughtsTokenCount": d,
                    "toolUsePromptTokenCount": e,
                }
            },
            _N,
            _N,
            _N,
            _N,
            _N,
        ),
        st.builds(
            lambda a: {
                "durationMilliseconds": a,
                "combinedPhrases": [{"text": "x"}],
                "phrases": [],
            },
            _N,
        ),
    )


# Whole documents of the shapes the adapters read, with `usage` in each spelling.
_DOCS = _usage_obj().flatmap(
    lambda u: st.sampled_from(["usage", "usageMetadata", "root", "message", "response"]).map(
        lambda where: {
            "usage": {"usage": u},
            "usageMetadata": {"usageMetadata": u},
            "root": u,
            "message": {"message": {"usage": u}},
            "response": {"response": {"usage": u}},
        }[where]
    )
)
_JUNK_DOCS = st.one_of(
    st.lists(_NUMBERS, max_size=4),
    _NUMBERS,
    st.dictionaries(st.text(max_size=5), _NUMBERS, max_size=4),
    st.sampled_from([5, 100, 900]).map(_deep),
    st.sampled_from([5, 100, 900]).map(lambda d: {"usage": _deep(d)}),
)


@st.composite
def bodies(draw: st.DrawFn) -> bytes:
    doc = draw(st.one_of(_shaped(), _shaped(), _shaped(), _DOCS, _JUNK_DOCS))
    text = json.dumps(doc, allow_nan=True)
    mode = draw(st.sampled_from(["plain", "plain", "plain", "truncate", "garbage", "deep", "utf8"]))
    if mode == "truncate":
        return text.encode()[: draw(st.integers(0, len(text)))]
    if mode == "garbage":
        return draw(st.binary(max_size=60))
    if mode == "deep":
        return b"[" * draw(st.sampled_from([200, 1500, 100_000])) + text.encode()
    if mode == "utf8":
        return text.encode() + b"\xff\xfe"
    return text.encode()


RETRY_AFTER = st.one_of(
    st.integers(-10, 10**7).map(str),
    st.sampled_from(
        [
            "1.5",
            "0.0001",
            "  7 ",
            "\u00b2",
            "\u0661\u0662",
            "1e999",
            "-1",
            "+5",
            "Wed, 21 Oct 2026 07:28:00 GMT",
            "",
            "inf",
            "nan",
            "0x10",
            "9" * 4000,
            "1." + "0" * 100 + "1",
            "86400",
            "86401",
            "1_0",
        ]
    ),
    st.text(max_size=12),
    st.floats().map(repr),
)
HEADERS = st.dictionaries(
    st.sampled_from(
        [
            "retry-after",
            "Retry-After",
            "RETRY-AFTER",
            "retry-after-ms",
            "character-cost",
            "x-amzn-ErrorType",
            "content-type",
        ]
    ),
    RETRY_AFTER | st.sampled_from(["ThrottlingException:http://x", "audio/mpeg"]),
    max_size=3,
)
STATUS = st.one_of(
    st.sampled_from(
        [200, 201, 204, 400, 401, 402, 403, 404, 408, 409, 413, 422, 429, 500, 502, 503, 504, 529]
    ),
    st.integers(-5, 1000),
)
ENDPOINTS = st.sampled_from(
    [
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/embeddings",
        "/v1/audio/speech",
        "/v1/audio/transcriptions",
        ANTHROPIC_PATH,
    ]
)


@st.composite
def requests(draw: st.DrawFn) -> CleanRequest:
    endpoint = draw(ENDPOINTS)
    body: dict[str, Any] = {"input": draw(st.text(max_size=50)), "model": "m"}
    if draw(st.booleans()):
        body["stream_options"] = {"include_usage": draw(st.booleans())}
    return CleanRequest(
        endpoint,
        "m",
        body,
        stream=draw(st.booleans()),
        files={"file": _WAV} if endpoint.endswith("transcriptions") else {},
        response_id="resp_1",
        created=1_700_000_000,
    )


def valid_usage(usage: Usage | None) -> None:
    if usage is None:
        return
    assert isinstance(usage, Usage)
    for name in usage.__dataclass_fields__:
        value = getattr(usage, name)
        if isinstance(value, Decimal):
            assert value.is_finite() and 0 <= value <= INT64_MAX, (name, value)
        else:
            assert isinstance(value, int) and not isinstance(value, bool)
            assert 0 <= value <= INT64_MAX, (name, value)


def check_adapters(case: tuple[CleanRequest, int, dict[str, str], bytes]) -> None:
    req, status, headers, body = case
    resp = UpstreamResponse(status, headers, body)
    for name, provider in PROVIDERS.items():
        valid_usage(provider.usage(req, resp))
        error = provider.error(resp)
        assert isinstance(error, UpstreamError), name
        hint = error.retry_after_seconds
        assert hint is None or 0 <= hint <= 86_400, (name, hint)
        assert isinstance(provider.response(req, resp), UpstreamResponse), name


def check_retry_after(case: tuple[str, str]) -> None:
    seconds, millis = case
    for result in (
        retry_after_seconds({"Retry-After": seconds}),
        retry_after_ms_seconds({"retry-after-ms": millis}),
    ):
        assert result is None or 0 <= result <= 86_400, (seconds, millis, result)


def check_shared_readers(doc: Any) -> None:  # noqa: ANN401
    for endpoint in sorted(shapes.ENDPOINTS):
        valid_usage(parse_usage(doc, endpoint))
    valid_usage(converse_usage(doc))


# --- streams -----------------------------------------------------------------------------------


@st.composite
def sse_streams(draw: st.DrawFn) -> bytes:
    """SSE with usage-bearing events (all the event names the readers key on) and noise."""
    out = b""
    for _ in range(draw(st.integers(1, 6))):
        usage = draw(
            st.one_of(
                _usage_obj(), _shaped().map(lambda d: next(iter(d.values())) if len(d) == 1 else d)
            )
        )
        name = draw(
            st.sampled_from(
                [
                    "message_start",
                    "message_delta",
                    "message_stop",
                    "ping",
                    "response.completed",
                    "x",
                    None,
                ]
            )
        )
        payload = draw(
            st.sampled_from(
                [
                    {"usage": usage},
                    {"message": {"usage": usage}},
                    {"response": {"usage": usage}},
                    {"usageMetadata": usage, "candidates": []},
                    {"choices": [], "usage": usage},
                ]
            )
        )
        text = json.dumps(payload, allow_nan=True)
        if draw(st.integers(0, 9)) == 0:
            text = draw(
                st.sampled_from(["[DONE]", "{", "null", "[]", "\u00e9", text[: len(text) // 2]])
            )
        out += (f"event: {name}\n" if name else "").encode() + b"data: " + text.encode() + b"\n\n"
    if draw(st.integers(0, 6)) == 0:
        out += draw(st.binary(max_size=30))
    return out


@st.composite
def event_streams(draw: st.DrawFn) -> bytes:
    """Bedrock event-stream frames whose payloads are hostile Converse events."""
    out = b""
    for _ in range(draw(st.integers(1, 5))):
        usage = draw(
            st.one_of(
                _usage_obj(), _shaped().map(lambda d: next(iter(d.values())) if len(d) == 1 else d)
            )
        )
        kind = draw(
            st.sampled_from(
                [
                    "metadata",
                    "metadata",
                    "contentBlockDelta",
                    "messageStop",
                    "contentBlockStart",
                    "messageStart",
                    "weird",
                ]
            )
        )
        payload: Any = draw(
            st.sampled_from(
                [
                    {"usage": usage},
                    {"delta": {"text": "x"}, "contentBlockIndex": draw(_NUMBERS)},
                    {"stopReason": draw(_NUMBERS)},
                    {
                        "start": {"toolUse": {"toolUseId": draw(_NUMBERS), "name": draw(_NUMBERS)}},
                        "contentBlockIndex": draw(_NUMBERS),
                    },
                    usage,
                    [1],
                    "str",
                    None,
                ]
            )
        )
        text = json.dumps(payload, allow_nan=True).encode()
        if draw(st.integers(0, 12)) == 0:
            text = draw(st.binary(max_size=30))
        head = header(":message-type", 7, b"event") + header(":event-type", 7, kind.encode())
        out += frame(head, text)
    return out


def split(data: bytes, cuts: list[int]) -> list[bytes]:
    points = sorted({c % (len(data) + 1) for c in cuts} | {0, len(data)})
    return [data[a:b] for a, b in itertools.pairwise(points)]


def check_relays(case: tuple[str, bytes, list[int], bool, bool]) -> None:
    name, wire, cuts, include_usage, tiny = case
    provider = PROVIDERS[name]
    req = CleanRequest(
        "/v1/messages" if name == "anthropic" else "/v1/chat/completions",
        "m",
        {"stream": True, "stream_options": {"include_usage": include_usage}},
        stream=True,
        response_id="r",
        created=1,
    )
    limits = (
        SseLimits(max_line_bytes=200, max_event_bytes=500, max_events=8, max_stream_bytes=3000)
        if tiny
        else SseLimits()
    )
    relay = provider.relay(req, limits)
    try:
        for chunk in split(wire, cuts):
            assert isinstance(relay.feed(chunk), bytes)
        assert isinstance(relay.close(), bytes)
    except StreamBroken:
        pass
    valid_usage(relay.usage())


_RELAYS: st.SearchStrategy[tuple[str, bytes, list[int], bool, bool]] = st.one_of(
    st.tuples(
        st.sampled_from(["openai", "azure_openai", "anthropic", "google"]),
        sse_streams(),
        st.lists(st.integers(0, 5000), max_size=5),
        st.booleans(),
        st.booleans(),
    ),
    st.tuples(
        st.just("bedrock"),
        event_streams(),
        st.lists(st.integers(0, 5000), max_size=5),
        st.booleans(),
        st.booleans(),
    ),
    st.tuples(
        st.just("bedrock"), st.binary(max_size=200), st.just([]), st.booleans(), st.booleans()
    ),
)


def test_fuzz_usage_readers() -> None:
    zlib.crc32(b"")  # frames are built with real CRCs by tests.fuzz.test_bedrock_stream
    run_fuzz(
        "usage_readers",
        [
            Case(
                "adapter_usage_error_response",
                st.tuples(requests(), STATUS, HEADERS, bodies()),
                check_adapters,
            ),
            Case("retry_after_parsers", st.tuples(RETRY_AFTER, RETRY_AFTER), check_retry_after),
            Case(
                "shared_readers_on_any_json",
                _DOCS | _JUNK_DOCS | _usage_obj(),
                check_shared_readers,
            ),
            Case("stream_relays_and_their_usage", _RELAYS, check_relays),
        ],
    )
