"""Gemini response-side shapes shared by the plain and streamed paths, plus JSON helpers.

Kept apart from `google.py` for size. It imports only `core` and `providers.port`.
"""

import json
from collections.abc import Mapping

from spendgate.core.cost import Usage
from spendgate.providers.port import Json, bounded, count_of, load_json

_FINISH = {"STOP": "stop", "MAX_TOKENS": "length"} | dict.fromkeys(
    ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "LANGUAGE"),
    "content_filter",
)
_USAGE = ("prompt", "toolUsePrompt", "cachedContent", "candidates", "thoughts")  # + TokenCount


def dig(value: Json, *path: str) -> Json:
    """Follow object keys; anything missing or not an object ends in None."""
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def as_int(value: Json) -> int | None:
    return count_of(value)  # non-negative and int64-sized; anything else is unreadable


def as_text(value: Json) -> str:
    return value if isinstance(value, str) else ""


def as_list(value: Json) -> list[Json]:
    return value if isinstance(value, list) else []


def load(raw: bytes | str) -> Mapping[str, Json] | None:
    data = load_json(raw)
    return data if isinstance(data, Mapping) else None


def read_usage(meta: Json) -> Usage | None:
    """Normalise `usageMetadata`: Gemini's candidates exclude thoughts, our output includes them."""
    if not isinstance(meta, Mapping):
        return None
    counts = [as_int(meta.get(f"{k}TokenCount", 0)) for k in _USAGE]
    if any(c is None for c in counts):
        return None
    prompt, tool_prompt, cached, candidates, thoughts = (c or 0 for c in counts)
    # Tool-use prompt tokens are input (never under-state); thoughts also feed the reasoning meter.
    return bounded(
        Usage(
            input_tokens=prompt + tool_prompt,
            output_tokens=candidates + thoughts,
            cached_input_tokens=cached,
            reasoning_tokens=thoughts,
        )
    )


def chat_usage(usage: Usage) -> Json:
    return {
        "prompt_tokens": usage.input_tokens,
        "completion_tokens": usage.output_tokens,
        "total_tokens": usage.input_tokens + usage.output_tokens,
        "prompt_tokens_details": {"cached_tokens": usage.cached_input_tokens},
        "completion_tokens_details": {"reasoning_tokens": usage.reasoning_tokens},
    }


def _tool_call(part: Json, position: int) -> dict[str, Json]:
    call: dict[str, Json] = {
        "id": as_text(dig(part, "functionCall", "id")) or f"call_{position}",
        "type": "function",
        "function": {
            "name": dig(part, "functionCall", "name"),
            "arguments": json.dumps(dig(part, "functionCall", "args") or {}),
        },
    }
    if isinstance(signature := dig(part, "thoughtSignature"), str):
        call["extra_content"] = {"google": {"thought_signature": signature}}
    return call


def candidate(chunk: Mapping[str, Json]) -> tuple[str, list[dict[str, Json]], str | None]:
    """(text, tool calls, OpenAI finish reason or None) of the first candidate of one chunk."""
    first = next(iter(as_list(chunk.get("candidates"))), None)
    text = ""
    calls: list[dict[str, Json]] = []
    for part in as_list(dig(first, "content", "parts")):
        if dig(part, "functionCall") is not None:
            calls.append(_tool_call(part, len(calls)))
        elif not dig(part, "thought"):  # thought summaries are not part of the answer
            text += as_text(dig(part, "text"))
    reason = as_text(dig(first, "finishReason"))
    if not reason:
        # A blocked prompt has no candidates, only `promptFeedback.blockReason`.
        blocked = as_text(dig(chunk, "promptFeedback", "blockReason"))
        return text, calls, "content_filter" if blocked else None
    return text, calls, "tool_calls" if calls and reason == "STOP" else _FINISH.get(reason, "stop")


def completion(chunk: Mapping[str, Json], model: str, response_id: str, created: int) -> Json:
    """A complete `generateContent` answer as an OpenAI `chat.completion` body."""
    text, calls, finish = candidate(chunk)
    message: dict[str, Json] = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = list[Json](calls)
    body: dict[str, Json] = {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish or "stop"}],
    }
    if (usage := read_usage(chunk.get("usageMetadata"))) is not None:
        body["usage"] = chat_usage(usage)
    return body
