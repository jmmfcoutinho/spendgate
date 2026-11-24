"""The `StreamRelay.usage()` contract (M-13): only FINAL, end-of-stream usage is reported.

Every streaming adapter gets a stream that carries usage mid-stream and then ends without its final
usage event (a cut, cancel or gateway stop). `usage()` must be None so the pipeline charges the full
hold. The pipeline trusts this contract rather than ignoring usage on every broken ending, because
a stream cut just after its final usage arrived is correctly charged that usage.
"""

import json
from collections.abc import Callable

import pytest

from spendgate.providers.anthropic import AnthropicProvider
from spendgate.providers.azure_openai import AzureOpenAIProvider
from spendgate.providers.bedrock import Bedrock
from spendgate.providers.google import GoogleProvider
from spendgate.providers.openai import OpenAIProvider
from spendgate.providers.port import CleanRequest, Json, Provider
from tests.fakes.frames import eventstream_frame


def _sse(payload: object, event: str | None = None) -> bytes:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(payload)}\n\n".encode()


_CHAT_RUNNING = _sse(
    {
        "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
    }
)
_RESPONSES_RUNNING = _sse(
    {
        "type": "response.in_progress",
        "response": {"usage": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11}},
    },
    "response.in_progress",
)
_ANTHROPIC_START = _sse(
    {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}},
    "message_start",
)
_GEMINI_RUNNING = _sse(
    {
        "candidates": [{"content": {"role": "model", "parts": [{"text": "x"}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 1},
    }
)
_BEDROCK_DELTA = eventstream_frame(
    "contentBlockDelta",
    {"contentBlockIndex": 0, "delta": {"text": "x"}, "usage": {"inputTokens": 10}},
)

# (provider, endpoint, a stream that carried usage mid-stream and then stopped)
CASES: dict[str, tuple[Callable[[], Provider], str, bytes]] = {
    "openai_chat": (OpenAIProvider, "/v1/chat/completions", _CHAT_RUNNING),
    "openai_responses": (OpenAIProvider, "/v1/responses", _RESPONSES_RUNNING),
    "azure_openai_chat": (AzureOpenAIProvider, "/v1/chat/completions", _CHAT_RUNNING),
    "azure_openai_responses": (AzureOpenAIProvider, "/v1/responses", _RESPONSES_RUNNING),
    "anthropic": (AnthropicProvider, "/v1/messages", _ANTHROPIC_START),
    "bedrock": (Bedrock, "/v1/chat/completions", _BEDROCK_DELTA),
    "google": (GoogleProvider, "/v1/chat/completions", _GEMINI_RUNNING),
}


@pytest.mark.parametrize("case", list(CASES))
def test_m13_mid_stream_usage_without_the_final_event_is_not_reported(case: str) -> None:
    make, endpoint, wire = CASES[case]
    body: dict[str, Json] = {"stream": True, "stream_options": {"include_usage": True}}
    req = CleanRequest(endpoint, "m", body, stream=True, response_id="r", created=1)
    relay = make().relay(req)
    relay.feed(wire)  # then the stream stops: close() is never called
    assert relay.usage() is None
