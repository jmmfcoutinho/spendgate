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
# Gemini finish reasons that end a candidate. FINISH_REASON_UNSPECIFIED (or any reason not
# listed) does not: usage sent with it is not trusted as final, so a cut charges the hold.
_TERMINAL = frozenset(
    {
        "STOP", "MAX_TOKENS", "SAFETY", "RECITATION", "LANGUAGE", "OTHER", "BLOCKLIST",
        "PROHIBITED_CONTENT", "SPII", "MALFORMED_FUNCTION_CALL", "IMAGE_SAFETY",
        "UNEXPECTED_TOOL_CALL", "TOO_MANY_TOOL_CALLS",
    }
)  # fmt: skip
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


_COUNTERS = (*(f"{k}TokenCount" for k in _USAGE), "totalTokenCount")


def shrinks(before: Json, after: Json) -> bool:
    """True when a counter of `after` is below that of `before` (absent counts as 0).

    Gemini's stream totals are cumulative and only grow; a drop means a counter went missing or
    the stream is not one answer, so the totals are not trusted.
    """
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return False
    return any((as_int(after.get(k, 0)) or 0) < (as_int(before.get(k, 0)) or 0) for k in _COUNTERS)


def has_output(chunk: Mapping[str, Json]) -> bool:
    """True when any candidate carries content parts (text, tool calls, thoughts, anything).

    Deliberately broad: any part, even a thought or an empty text, makes a missing candidates
    count unreadable (the full hold). That over-charges a thoughts-only reply without the count,
    but never under-charges a part kind this adapter does not list (images, code results).
    """
    return any(as_list(dig(c, "content", "parts")) for c in as_list(chunk.get("candidates")))


def read_usage(meta: Json, *, output_seen: bool) -> Usage | None:
    """Normalise `usageMetadata`: Gemini's candidates exclude thoughts, our output includes them.

    `promptTokenCount` is required. Gemini leaves `candidatesTokenCount` out when it is zero, so
    it may be absent only when no candidate carried content (`output_seen` False: a blocked
    prompt, a reply cut while thinking); after content an absent count is unreadable, never 0.
    The other counters are optional but must be counts when present. A present
    `totalTokenCount` larger than its parts means a part is missing: unreadable too.
    """
    if not isinstance(meta, Mapping) or "promptTokenCount" not in meta:
        return None
    if output_seen and "candidatesTokenCount" not in meta:
        return None
    counts = [as_int(meta.get(f"{k}TokenCount", 0)) for k in _USAGE]
    if any(c is None for c in counts):
        return None
    prompt, tool_prompt, cached, candidates, thoughts = (c or 0 for c in counts)
    if "totalTokenCount" in meta:
        total = as_int(meta["totalTokenCount"])
        if total is None or total > prompt + tool_prompt + candidates + thoughts:
            return None
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


def finishes(chunk: Mapping[str, Json]) -> bool:
    """True when this chunk ends the answer: a known terminal finish reason or a blocked prompt."""
    first = next(iter(as_list(chunk.get("candidates"))), None)
    if as_text(dig(first, "finishReason")) in _TERMINAL:
        return True
    return bool(as_text(dig(chunk, "promptFeedback", "blockReason")))


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
    if (usage := read_usage(chunk.get("usageMetadata"), output_seen=has_output(chunk))) is not None:
        body["usage"] = chat_usage(usage)
    return body
