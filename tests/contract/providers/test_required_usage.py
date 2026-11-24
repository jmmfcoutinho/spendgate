"""Every adapter reads usage with a required counter missing as unreadable (None, M-13).

None is what the pipeline settles as the full hold; a missing counter read as 0 would book work
the provider did as free. Optional counters (cached, reasoning, audio, cache writes) may be
absent, but when present they must be non-negative ints. Present zeros are a real reading. The
whole-proxy cases are in `tests/integration/test_missing_usage.py`; here are the streams and the
endpoints the rig has no catalog entry for.
"""

import json
from collections.abc import Mapping

import pytest

from spendgate.core.cost import Usage
from spendgate.providers import openai_shapes as shapes
from spendgate.providers.anthropic import AnthropicProvider
from spendgate.providers.azure_openai import AzureOpenAIProvider
from spendgate.providers.google import GoogleProvider
from spendgate.providers.google_shapes import read_usage
from spendgate.providers.openai import OpenAIProvider
from spendgate.providers.openai_usage import parse_usage
from spendgate.providers.port import CleanRequest, Json, Provider, SseEvent, UpstreamResponse
from spendgate.providers.sse import serialise

OPENAI_FAMILY: list[Provider] = [OpenAIProvider(), AzureOpenAIProvider()]
GEMINI_TEXT: Json = [{"content": {"role": "model", "parts": [{"text": "Hi"}]}}]
GEMINI_STOP: dict[str, Json] = {
    "candidates": [{"content": {"parts": [{"text": "!"}]}, "finishReason": "STOP"}]
}


def answer(body: Mapping[str, object]) -> UpstreamResponse:
    return UpstreamResponse(200, {}, json.dumps(body).encode())


def read(provider: Provider, endpoint: str, body: Mapping[str, object]) -> Usage | None:
    return provider.usage(CleanRequest(endpoint, "m", {"input": "hi"}), answer(body))


def streamed(provider: Provider, endpoint: str, events: list[SseEvent]) -> Usage | None:
    relay = provider.relay(CleanRequest(endpoint, "m", {}, stream=True))
    for event in events:
        relay.feed(serialise(event))
    relay.close()
    return relay.usage()


def sse(payload: Mapping[str, object], name: str | None = None) -> SseEvent:
    return SseEvent(name, json.dumps(payload))


# --- OpenAI and Azure OpenAI ---------------------------------------------------------------------

OPENAI_MISSING = [
    (shapes.CHAT, {"prompt_tokens": 12, "total_tokens": 4012}),
    (shapes.CHAT, {"completion_tokens": 9}),
    (shapes.RESPONSES, {"input_tokens": 12, "total_tokens": 4012}),
    (shapes.RESPONSES, {"output_tokens": 9}),
    (shapes.EMBEDDINGS, {"total_tokens": 12}),
    (shapes.TRANSCRIPTIONS, {"type": "tokens", "input_tokens": 12, "total_tokens": 12}),
    (shapes.TRANSCRIPTIONS, {"type": "duration"}),
]
OPENAI_BAD_OPTIONAL = [
    {"prompt_tokens_details": {"audio_tokens": None}},  # nulls that could raise the cost
    {"prompt_tokens_details": {"cache_write_tokens": None}},
    {"completion_tokens_details": {"reasoning_tokens": None}},
    {"completion_tokens_details": {"audio_tokens": None}},
    {"prompt_tokens_details": {"cached_tokens": -1}},
    {"prompt_tokens_details": {"audio_tokens": "3"}},
    {"completion_tokens_details": {"reasoning_tokens": 1.5}},
    {"completion_tokens_details": {"audio_tokens": True}},
    {"prompt_tokens_details": "cached"},
    {"completion_tokens_details": [1]},
]


@pytest.mark.parametrize("provider", OPENAI_FAMILY, ids=["openai", "azure_openai"])
@pytest.mark.parametrize(("endpoint", "usage"), OPENAI_MISSING)
def test_m13_openai_usage_without_a_required_counter_is_unreadable(
    provider: Provider, endpoint: str, usage: dict[str, Json]
) -> None:
    assert read(provider, endpoint, {"usage": usage}) is None


@pytest.mark.parametrize("provider", OPENAI_FAMILY, ids=["openai", "azure_openai"])
def test_m13_openai_present_zero_counters_are_a_reading(provider: Provider) -> None:
    zero = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    assert read(provider, shapes.CHAT, {"usage": zero}) == Usage()
    assert read(provider, shapes.RESPONSES, {"usage": {"input_tokens": 0, "output_tokens": 0}})
    assert read(provider, shapes.EMBEDDINGS, {"usage": {"prompt_tokens": 0}}) == Usage()
    tokens = {"type": "tokens", "input_tokens": 0, "output_tokens": 0}
    assert read(provider, shapes.TRANSCRIPTIONS, {"usage": tokens}) == Usage()


def test_m13_embeddings_need_no_output_counter_but_chat_does() -> None:
    usage: Json = {"prompt_tokens": 12, "total_tokens": 12}
    assert parse_usage(usage, shapes.EMBEDDINGS) == Usage(input_tokens=12)
    assert parse_usage({"input_tokens": 12}, shapes.EMBEDDINGS) == Usage(input_tokens=12)
    assert parse_usage(usage, shapes.CHAT) is None


DURATION: dict[str, Json] = {"type": "duration", "seconds": 0}
TOKENS: dict[str, Json] = {"prompt_tokens": 10_000, "completion_tokens": 5_000}


@pytest.mark.parametrize("endpoint", [shapes.CHAT, shapes.RESPONSES, shapes.EMBEDDINGS, "/v1/x"])
def test_m13_usage_of_another_endpoints_shape_is_unreadable(endpoint: str) -> None:
    """A free duration usage, even beside token counts, prices only a transcription."""
    assert parse_usage(DURATION, endpoint) is None
    assert parse_usage(DURATION | TOKENS, endpoint) is None
    assert parse_usage(DURATION, shapes.TRANSCRIPTIONS) == Usage()
    assert parse_usage(TOKENS, shapes.RESPONSES) is None  # chat names on a Responses answer
    assert parse_usage({"input_tokens": 1, "output_tokens": 1}, shapes.CHAT) is None
    assert (
        parse_usage({"type": "words", "input_tokens": 1, "output_tokens": 1}, shapes.TRANSCRIPTIONS)
        is None
    )
    assert parse_usage(TOKENS, shapes.SPEECH) is None


@pytest.mark.parametrize("bad", OPENAI_BAD_OPTIONAL)
def test_m13_openai_optional_counters_must_be_counts_when_present(bad: dict[str, Json]) -> None:
    base: dict[str, Json] = {"prompt_tokens": 12, "completion_tokens": 9}
    assert parse_usage(base, shapes.CHAT) == Usage(input_tokens=12, output_tokens=9)
    no_details = base | {"prompt_tokens_details": None}
    assert parse_usage(no_details, shapes.CHAT) is not None  # null: no details
    assert parse_usage(base | bad, shapes.CHAT) is None


def test_m13_a_null_cache_read_count_is_zero_but_a_null_required_count_is_not() -> None:
    """Null cache reads over-charge when read as 0 (safe); a null required count would not."""
    cached: Json = {
        "prompt_tokens": 12,
        "completion_tokens": 9,
        "prompt_tokens_details": {"cached_tokens": None},
    }
    assert parse_usage(cached, shapes.CHAT) == Usage(input_tokens=12, output_tokens=9)
    responses: Json = {
        "input_tokens": 12,
        "output_tokens": 9,
        "input_tokens_details": {"cached_tokens": None},
    }
    assert parse_usage(responses, shapes.RESPONSES) == Usage(input_tokens=12, output_tokens=9)
    assert parse_usage({"prompt_tokens": 12, "completion_tokens": None}, shapes.CHAT) is None
    assert parse_usage({"prompt_tokens": None, "completion_tokens": 9}, shapes.CHAT) is None
    assert parse_usage({"prompt_tokens": None}, shapes.EMBEDDINGS) is None


def test_m13_openai_streamed_usage_without_a_required_counter_is_unreadable() -> None:
    provider = OpenAIProvider()
    chunk = {"id": "c", "choices": [], "usage": {"prompt_tokens": 12, "total_tokens": 4012}}
    assert streamed(provider, shapes.CHAT, [sse(chunk), SseEvent(None, "[DONE]")]) is None
    done = {"type": "response.completed", "response": {"usage": {"input_tokens": 12}}}
    assert streamed(provider, shapes.RESPONSES, [sse(done, "response.completed")]) is None
    zero: Json = {"input_tokens": 0, "output_tokens": 0}
    whole: dict[str, Json] = {"type": "response.completed", "response": {"usage": zero}}
    assert streamed(provider, shapes.RESPONSES, [sse(whole, "response.completed")]) == Usage()


def test_m13_openai_stream_with_a_later_unreadable_usage_is_unreadable() -> None:
    """A good usage followed by one that does not parse: the earlier one cannot stand."""
    good = sse({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    bad = sse({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": "4000"}})
    done = SseEvent(None, "[DONE]")
    for provider in OPENAI_FAMILY:
        assert streamed(provider, shapes.CHAT, [good, done]) == Usage(1, 1)
        assert streamed(provider, shapes.CHAT, [good, bad]) is None
        assert streamed(provider, shapes.CHAT, [bad, good]) is None


def test_m13_speech_without_a_text_input_has_no_usage() -> None:
    for provider in OPENAI_FAMILY:
        req = CleanRequest(shapes.SPEECH, "m", {"input": ["not", "text"]})
        assert provider.usage(req, UpstreamResponse(200, {}, b"audio")) is None
        assert (
            provider.usage(CleanRequest(shapes.SPEECH, "m", {"input": ""}), answer({})) == Usage()
        )


# --- Anthropic -----------------------------------------------------------------------------------

MESSAGES = "/v1/messages"


@pytest.mark.parametrize(
    "usage",
    [{"input_tokens": 12}, {"output_tokens": 9}, {}, {"input_tokens": 12, "output_tokens": None}],
)
def test_m13_anthropic_usage_without_a_required_counter_is_unreadable(
    usage: dict[str, Json],
) -> None:
    assert read(AnthropicProvider(), MESSAGES, {"usage": usage}) is None


def test_m13_anthropic_present_zero_counters_are_a_reading() -> None:
    zero = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    assert read(AnthropicProvider(), MESSAGES, {"usage": zero}) == Usage()


def test_m13_anthropic_stream_without_a_required_counter_is_unreadable() -> None:
    provider = AnthropicProvider()
    start = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 12}}}, "message_start"
    )
    no_input = sse({"type": "message_start", "message": {"usage": {}}}, "message_start")
    delta = sse({"type": "message_delta", "usage": {"output_tokens": 9}}, "message_delta")
    no_output = sse({"type": "message_delta", "usage": {}}, "message_delta")
    assert streamed(provider, MESSAGES, [start, delta]) == Usage(input_tokens=12, output_tokens=9)
    assert streamed(provider, MESSAGES, [no_input, delta]) is None
    assert streamed(provider, MESSAGES, [start, no_output]) is None
    assert streamed(provider, MESSAGES, [start]) is None  # no final output count at all


# --- Gemini --------------------------------------------------------------------------------------

GEMINI_MISSING = [
    {},
    {"candidatesTokenCount": 9},
    {"promptTokenCount": 12},  # text came back, so the absent count is not a zero
    {"promptTokenCount": 12, "candidatesTokenCount": 9, "thoughtsTokenCount": None},
    {"promptTokenCount": 12, "candidatesTokenCount": 9, "cachedContentTokenCount": -1},
]


GEMINI_MISSING += [  # a present total larger than its parts: a part is missing
    {"promptTokenCount": 5, "totalTokenCount": 5000},
    {
        "promptTokenCount": 5,
        "candidatesTokenCount": 2,
        "thoughtsTokenCount": 1,
        "totalTokenCount": 9,
    },
    {"promptTokenCount": 5, "candidatesTokenCount": 2, "totalTokenCount": None},
]


@pytest.mark.parametrize("meta", GEMINI_MISSING)
def test_m13_gemini_usage_without_a_required_counter_is_unreadable(meta: dict[str, Json]) -> None:
    body: dict[str, Json] = {"candidates": GEMINI_TEXT, "usageMetadata": meta}
    assert read(GoogleProvider(), shapes.CHAT, body) is None


def test_m13_gemini_may_omit_the_candidates_count_only_without_candidates_content() -> None:
    meta: Json = {"promptTokenCount": 12, "thoughtsTokenCount": 4}
    thought_out: Json = [{"finishReason": "MAX_TOKENS", "content": {"role": "model"}}]
    assert read_usage(meta, output_seen=False) == Usage(
        input_tokens=12, output_tokens=4, reasoning_tokens=4
    )
    assert read_usage(meta, output_seen=True) is None
    blocked: dict[str, Json] = {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": meta}
    assert read(GoogleProvider(), shapes.CHAT, blocked) is not None
    assert read(GoogleProvider(), shapes.CHAT, blocked | {"candidates": thought_out}) is not None
    zero = {"promptTokenCount": 0, "candidatesTokenCount": 0}
    assert read(GoogleProvider(), shapes.CHAT, {"candidates": GEMINI_TEXT, "usageMetadata": zero})


def test_m13_gemini_stream_judges_the_final_totals_against_all_content_seen() -> None:
    """Final totals may come after the text: their missing candidates count is still no zero."""
    text = sse({"candidates": GEMINI_TEXT})
    stop_empty = sse({"candidates": [{"finishReason": "STOP"}]})  # the finish, no parts of its own
    prompt_only = sse({"usageMetadata": {"promptTokenCount": 12}})
    totals = sse({"usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 2}})
    blocked = sse(
        {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": 12}}
    )
    google = GoogleProvider()
    assert streamed(google, shapes.CHAT, [text, sse(GEMINI_STOP), prompt_only]) is None
    assert streamed(google, shapes.CHAT, [text, stop_empty, prompt_only]) is None
    assert streamed(google, shapes.CHAT, [stop_empty, prompt_only]) == Usage(input_tokens=12)
    assert streamed(google, shapes.CHAT, [blocked]) == Usage(input_tokens=12)
    assert streamed(google, shapes.CHAT, [text, sse(GEMINI_STOP), totals]) == Usage(12, 2)


def test_m13_gemini_total_must_not_exceed_its_parts() -> None:
    parts: dict[str, Json] = {"promptTokenCount": 5, "candidatesTokenCount": 2}
    thoughts = parts | {"thoughtsTokenCount": 3, "toolUsePromptTokenCount": 1}
    assert read_usage(parts | {"totalTokenCount": 7}, output_seen=True) == Usage(5, 2)
    assert read_usage(parts | {"totalTokenCount": 6}, output_seen=True) == Usage(5, 2)
    assert read_usage(thoughts | {"totalTokenCount": 11}, output_seen=True) is not None
    assert read_usage(thoughts | {"totalTokenCount": 12}, output_seen=True) is None
    blocked: dict[str, Json] = {"usageMetadata": {"promptTokenCount": 5, "totalTokenCount": 5000}}
    assert (
        read(GoogleProvider(), shapes.CHAT, blocked | {"candidates": [{"finishReason": "STOP"}]})
        is None
    )
    final = GEMINI_STOP | {"usageMetadata": parts | {"totalTokenCount": 4000}}
    assert streamed(GoogleProvider(), shapes.CHAT, [sse(final)]) is None
    honest = GEMINI_STOP | {"usageMetadata": parts | {"totalTokenCount": 7}}
    assert streamed(GoogleProvider(), shapes.CHAT, [sse(honest)]) == Usage(5, 2)


def test_m13_gemini_any_content_after_the_finish_makes_its_usage_untrusted_again() -> None:
    """Not only text: an image (or any part) after the finish means the answer went on."""
    final = GEMINI_STOP | {"usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}}
    image = sse(
        {
            "candidates": [
                {"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": "AA=="}}]}}
            ]
        }
    )
    google = GoogleProvider()
    assert streamed(google, shapes.CHAT, [sse(final)]) == Usage(5, 2)
    assert streamed(google, shapes.CHAT, [sse(final), image]) is None


def test_m13_gemini_totals_that_drop_are_unreadable_and_not_shown_to_the_client() -> None:
    """Totals only grow: a later chunk below an earlier one charges the hold, and the client's
    usage-only chunk is left out so it sees what is charged."""
    early = sse(
        {
            "candidates": GEMINI_TEXT,
            "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 40},
        }
    )
    final = GEMINI_STOP | {"usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 2}}
    google = GoogleProvider()
    assert streamed(google, shapes.CHAT, [early, sse(final)]) is None
    grown = GEMINI_STOP | {"usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 41}}
    assert streamed(google, shapes.CHAT, [early, sse(grown)]) == Usage(50, 41)
    relay = google.relay(
        CleanRequest(shapes.CHAT, "m", {"stream_options": {"include_usage": True}}, stream=True)
    )
    for event in (early, sse(final)):
        relay.feed(serialise(event))
    tail = relay.close()
    assert tail.endswith(b"data: [DONE]\n\n") and b'"usage"' not in tail
    shown = google.relay(
        CleanRequest(shapes.CHAT, "m", {"stream_options": {"include_usage": True}}, stream=True)
    )
    for event in (early, sse(grown)):
        shown.feed(serialise(event))
    assert b'"usage"' in shown.close()
