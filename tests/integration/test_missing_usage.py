"""A 200 whose usage lacks a required counter is unreadable, so it settles at the full hold (M-13).

One case per adapter and endpoint, through the whole proxy: the fake answers with a hand-written
body, and the ledger must show `ok_no_usage` at the hold. Reading the missing counter as 0 would
book a call that did work as (nearly) free. Counters that are present and zero are a real reading
and are accepted.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import pytest

from spendgate.core.outcomes import Outcome
from tests.fakes import Script
from tests.integration.harness import CHAT, MESSAGES_PATH, Stack, async_test, open_stack

type Doc = Mapping[str, object]

USER = [{"role": "user", "content": "hi"}]
GEMINI_TEXT = [{"content": {"role": "model", "parts": [{"text": "Hi"}]}, "finishReason": "STOP"}]
CONVERSE_TEXT = {"message": {"role": "assistant", "content": [{"text": "Hi"}]}}


@dataclass(frozen=True, slots=True)
class Case:
    """One endpoint of one adapter: the client request and the answer around a usage object."""

    path: str
    request: Doc
    answer: Doc
    usage_key: str = "usage"


OPENAI_CHAT = Case(
    CHAT, {"model": "alpha-one", "messages": USER}, {"object": "chat.completion", "choices": []}
)
RESPONSES = Case(
    "/v1/responses", {"model": "alpha-one", "input": "hi"}, {"object": "response", "output": []}
)
EMBEDDINGS = Case(
    "/v1/embeddings", {"model": "embed", "input": "hi"}, {"object": "list", "data": []}
)
MESSAGES = Case(
    MESSAGES_PATH,
    {"model": "haiku", "max_tokens": 50, "messages": USER},
    {"type": "message", "role": "assistant", "content": []},
)
BEDROCK = Case(CHAT, {"model": "bedrock-haiku", "messages": USER}, {"output": CONVERSE_TEXT})
GEMINI = Case(
    CHAT, {"model": "gemini", "messages": USER}, {"candidates": GEMINI_TEXT}, "usageMetadata"
)
NULL_AUDIO: Doc = {
    "prompt_tokens": 12,
    "completion_tokens": 9,
    "prompt_tokens_details": {"audio_tokens": None},  # dearer than text: a null cannot be 0
}

DURATION_AND_TOKENS: Doc = {
    "type": "duration",
    "seconds": 0,
    "prompt_tokens": 10_000,
    "completion_tokens": 5_000,
}

MISSING: dict[str, tuple[Case, Doc]] = {
    "openai_chat_with_a_transcription_duration_usage": (OPENAI_CHAT, DURATION_AND_TOKENS),
    "responses_with_a_transcription_duration_usage": (
        RESPONSES,
        {"type": "duration", "seconds": 0},
    ),
    "embeddings_with_a_transcription_duration_usage": (
        EMBEDDINGS,
        {"type": "duration", "seconds": 0},
    ),
    "openai_chat_without_completion_tokens": (OPENAI_CHAT, {"prompt_tokens": 12}),
    "openai_chat_without_prompt_tokens": (OPENAI_CHAT, {"completion_tokens": 9}),
    "openai_chat_with_a_null_audio_count": (OPENAI_CHAT, NULL_AUDIO),
    "responses_without_output_tokens": (RESPONSES, {"input_tokens": 12, "total_tokens": 4012}),
    "responses_without_input_tokens": (RESPONSES, {"output_tokens": 9, "total_tokens": 21}),
    "embeddings_without_prompt_tokens": (EMBEDDINGS, {"total_tokens": 12}),
    "anthropic_without_output_tokens": (MESSAGES, {"input_tokens": 12}),
    "anthropic_without_input_tokens": (MESSAGES, {"output_tokens": 9}),
    "bedrock_without_output_tokens": (BEDROCK, {"inputTokens": 12, "totalTokens": 12}),
    "gemini_empty_usage_metadata": (GEMINI, {}),
    "gemini_without_prompt_token_count": (GEMINI, {"candidatesTokenCount": 9}),
    "gemini_answer_without_candidates_token_count": (GEMINI, {"promptTokenCount": 12}),
    "gemini_total_larger_than_its_parts": (
        replace(GEMINI, answer={"candidates": [{"finishReason": "STOP"}]}),
        {"promptTokenCount": 5, "totalTokenCount": 5000},
    ),
}

ZERO: dict[str, tuple[Case, Doc]] = {
    "openai_chat": (OPENAI_CHAT, {"prompt_tokens": 0, "completion_tokens": 0}),
    "responses": (RESPONSES, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}),
    "embeddings": (EMBEDDINGS, {"prompt_tokens": 0, "total_tokens": 0}),
    "anthropic": (MESSAGES, {"input_tokens": 0, "output_tokens": 0}),
    "bedrock": (BEDROCK, {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}),
    "gemini": (GEMINI, {"promptTokenCount": 0, "candidatesTokenCount": 0}),
}


async def answer_with(s: Stack, case: Case, usage: Doc) -> httpx.Response:
    body = {**case.answer, case.usage_key: usage}
    s.fake.enqueue(Script(body=json.dumps(body).encode()))
    return await s.post(case.path, dict(case.request))


async def booked(s: Stack) -> tuple[Outcome, int, int]:
    """(outcome, charged, hold) of the only request; nothing may be left held."""
    held = s.observer.held[0]
    (row,) = await s.ledger.get_calls(held.request_id)
    spent, still_held = await s.totals()
    assert still_held == 0 and spent == row.charged_nano
    return row.outcome, row.charged_nano, held.hold.nano


@pytest.mark.parametrize("name", sorted(MISSING))
@async_test
async def test_m13_missing_usage_counter_charges_the_full_hold(tmp_path: Path, name: str) -> None:
    case, usage = MISSING[name]
    async with open_stack(tmp_path) as s:
        r = await answer_with(s, case, usage)
        assert r.status_code == 200, r.text
        outcome, charged, hold = await booked(s)
        assert (outcome, charged) == (Outcome.OK_NO_USAGE, hold)
        assert hold > 0


@pytest.mark.parametrize("name", sorted(ZERO))
@async_test
async def test_m13_present_zero_usage_counters_are_a_reading(tmp_path: Path, name: str) -> None:
    case, usage = ZERO[name]
    async with open_stack(tmp_path) as s:
        r = await answer_with(s, case, usage)
        assert r.status_code == 200, r.text
        assert await booked(s) == (Outcome.OK, 0, s.observer.held[0].hold.nano)


@async_test
async def test_m13_gemini_without_candidates_content_may_omit_the_candidates_count(
    tmp_path: Path,
) -> None:
    """A blocked prompt has no candidates and Gemini omits the zero count: the prompt is booked."""
    blocked = replace(GEMINI, answer={"promptFeedback": {"blockReason": "SAFETY"}})
    async with open_stack(tmp_path) as s:
        r = await answer_with(s, blocked, {"promptTokenCount": 12, "totalTokenCount": 12})
        assert r.status_code == 200, r.text
        outcome, charged, hold = await booked(s)
        assert outcome is Outcome.OK and 0 < charged < hold


@async_test
async def test_m13_a_null_cached_count_is_read_as_no_cache_reads(tmp_path: Path) -> None:
    """Cache reads only lower the price: a null one is 0, billing every input token in full."""
    details: Doc = {"cached_tokens": None}
    null_cached: Doc = {
        "prompt_tokens": 12,
        "completion_tokens": 9,
        "prompt_tokens_details": details,
    }
    async with open_stack(tmp_path) as s:
        r = await answer_with(s, OPENAI_CHAT, null_cached)
        assert r.status_code == 200, r.text
        outcome, charged, hold = await booked(s)
        assert outcome is Outcome.OK and 0 < charged < hold
