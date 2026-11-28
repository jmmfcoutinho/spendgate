"""OpenAI response side, shared with Azure OpenAI: usage readers, stream collector, errors.

Usage is normalised to `core.cost.Usage`: text counters keep their cached and reasoning subsets,
audio tokens move to the separate audio counters so nothing is billed twice. Anything that does
not parse cleanly is "no usage" (None), which the pipeline settles as the full hold.

Each endpoint accepts only its own usage shape, so a chat answer cannot pass for a free
transcription. The input and output counters are required (embeddings have no output): a missing
one is unreadable, never 0, or a call that produced output would book as free. The detail
counters are optional, since most answers have no cached, reasoning or audio part, but a present
one must be a count.
"""

import math
from collections.abc import Mapping
from decimal import Decimal

from spendgate.core.cost import Usage
from spendgate.core.errors import ErrorCode
from spendgate.providers import openai_shapes as shapes
from spendgate.providers.port import (
    INT64_MAX,
    Json,
    SseEvent,
    UpstreamError,
    UpstreamResponse,
    load_json,
    retry_after_seconds,
)

_CONTEXT_CODES = frozenset({"context_length_exceeded", "string_above_max_length"})
# `error.code` values that say our key, account or configured model is wrong whatever was sent,
# so the breaker counts them (ADR-0033). A 403 or 404 without one answered one request: OpenAI's
# "not allowed to generate embeddings from this model" 403 carries `code: null`.
DEPLOYMENT_FAULTS = frozenset(
    {"invalid_api_key", "model_not_found", "unsupported_country_region_territory"}
)
_MAX_MESSAGE = 500


def _count(
    obj: Mapping[str, Json], key: str, *, required: bool = False, null_is_zero: bool = False
) -> int:
    """A counter: absent is 0 only when optional; a present key must be a count.

    `null_is_zero` is only for a counter that can lower the cost (cached reads): reading its null
    as 0 bills those tokens at the full input price, which over-charges. A null counter that can
    raise the cost (audio, cache writes, reasoning) or a required one stays unreadable.
    """
    if key not in obj and not required:
        return 0
    value = obj.get(key)
    if value is None and null_is_zero and not required:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= INT64_MAX:
        raise ValueError(key)
    return value


def _details(usage: Mapping[str, Json], *keys: str) -> Mapping[str, Json]:
    """The first details object; null is "no details", any other non-object is unreadable."""
    for key in keys:
        value = usage.get(key)
        if isinstance(value, Mapping):
            return value
        if value is not None:
            raise ValueError(key)
    return {}


def _text_usage(u: Mapping[str, Json], in_key: str, out_key: str | None) -> Usage:
    """Chat (`prompt_tokens`/`completion_tokens`) and Responses (`input_tokens`/...) usage."""
    inp = _details(u, "prompt_tokens_details", "input_tokens_details", "input_token_details")
    out = _details(u, "completion_tokens_details", "output_tokens_details")
    audio_in, audio_out = _count(inp, "audio_tokens"), _count(out, "audio_tokens")
    return Usage(
        input_tokens=max(_count(u, in_key, required=True) - audio_in, 0),
        output_tokens=max((_count(u, out_key, required=True) if out_key else 0) - audio_out, 0),
        cached_input_tokens=_count(inp, "cached_tokens", null_is_zero=True),
        cache_write_tokens=_count(inp, "cache_write_tokens"),
        reasoning_tokens=_count(out, "reasoning_tokens"),
        audio_input_tokens=audio_in,
        audio_output_tokens=audio_out,
    )


def _duration(u: Mapping[str, Json]) -> Usage:
    seconds = u.get("seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, int | float):
        raise ValueError("seconds")
    if not math.isfinite(seconds) or not 0 <= seconds <= INT64_MAX:
        raise ValueError("seconds")
    return Usage(audio_seconds=Decimal(str(seconds)))


# Input and output counter names per endpoint (embeddings have no output). Transcription billed
# by tokens uses the Responses names; billed by duration it has its own shape (`_duration`).
_TOKEN_KEYS: Mapping[str, tuple[str, str | None]] = {
    shapes.CHAT: ("prompt_tokens", "completion_tokens"),
    shapes.RESPONSES: ("input_tokens", "output_tokens"),
    shapes.TRANSCRIPTIONS: ("input_tokens", "output_tokens"),
    shapes.EMBEDDINGS: ("prompt_tokens", None),
}


def _by_endpoint(u: Mapping[str, Json], endpoint: str) -> Usage | None:
    transcription = endpoint == shapes.TRANSCRIPTIONS
    if transcription and u.get("type") == "duration":
        return _duration(u)
    if "type" in u and not (transcription and u["type"] == "tokens"):
        return None  # only transcription usage is typed: this is not this endpoint's usage
    if (keys := _TOKEN_KEYS.get(endpoint)) is None:
        return None
    in_key, out_key = keys
    if endpoint == shapes.EMBEDDINGS and in_key not in u:
        in_key = "input_tokens"  # embeddings usage is spelled either way
    return _text_usage(u, in_key, out_key)


def parse_usage(value: Json, endpoint: str) -> Usage | None:
    """Read the usage object of an `endpoint` answer; any other shape is unreadable (None)."""
    if not isinstance(value, Mapping):
        return None
    try:
        return _by_endpoint(value, endpoint)
    except ValueError:
        return None


def usage_of(response: UpstreamResponse, endpoint: str) -> Usage | None:
    """Usage of a non-streamed answer. Speech is audio bytes, so it has none (None)."""
    body = load_json(response.body)
    return parse_usage(body.get("usage"), endpoint) if isinstance(body, Mapping) else None


# Responses events that end the answer; only their `response.usage` is the final usage.
_TERMINAL_EVENTS = frozenset({"response.completed", "response.incomplete", "response.failed"})


class OpenAIStreamUsage:
    """Keeps final usage only: chat's usage-only chunk, or a terminal Responses event.

    A compatible upstream may also put running totals on content chunks (or on
    `response.in_progress`); those are partial, so a stream cut after one must still be charged
    the full hold (M-13) and they are ignored here. A final usage object that does not parse
    breaks the reading for good: an earlier, smaller one must not stand in for it.
    """

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint
        self._usage: Usage | None = None
        self._broken = False

    def feed(self, event: SseEvent) -> None:
        obj = load_json(event.data)  # "[DONE]" and malformed events simply carry nothing
        if not isinstance(obj, Mapping):
            return
        holder = _final_holder(obj)
        if holder is None:
            if _continues(obj):
                self._usage = None  # the answer went on after its "final" usage: untrusted
            return
        if (raw := holder.get("usage")) is not None:
            found = parse_usage(raw, self._endpoint)
            self._broken = self._broken or found is None
            self._usage = found

    def result(self) -> Usage | None:
        return None if self._broken else self._usage


def _final_holder(obj: Mapping[str, Json]) -> Mapping[str, Json] | None:
    """The object whose `usage` is final in this event, or None for a mid-stream event."""
    response = obj.get("response")
    if isinstance(response, Mapping):
        return response if obj.get("type") in _TERMINAL_EVENTS else None
    return obj if obj.get("choices") == [] else None


def _continues(obj: Mapping[str, Json]) -> bool:
    """A chat chunk with choices, or any non-terminal Responses event: the answer is not over."""
    choices = obj.get("choices")
    return bool(choices) if isinstance(choices, list) else isinstance(obj.get("type"), str)


def is_usage_only_chunk(event: SseEvent) -> bool:
    """The final chat chunk that carries usage and no choices."""
    obj = load_json(event.data)
    return isinstance(obj, Mapping) and obj.get("choices") == [] and bool(obj.get("usage"))


# --- errors --------------------------------------------------------------------------------------


def map_error(
    response: UpstreamResponse, faults: frozenset[str] = DEPLOYMENT_FAULTS
) -> UpstreamError:
    """Core error for an OpenAI error answer (protocol.md section 4.2, rule 1).

    An `error.code` in `faults` marks a deployment fault (Azure passes its own codes as well).
    """
    status = response.status
    body = load_json(response.body)
    err = body.get("error") if isinstance(body, Mapping) else None
    err = err if isinstance(err, Mapping) else {}
    code, text, param = err.get("code"), err.get("message"), err.get("param")
    message = text[:_MAX_MESSAGE] if isinstance(text, str) else ""
    provider_code = code if isinstance(code, str) else ""
    mapped = _code_for(status, provider_code)
    retry = retry_after_seconds(response.headers) if status == 429 else None  # noqa: PLR2004
    del param  # provider param names are not ours; the pipeline reports its own
    return UpstreamError(mapped, True, status, retry, message, provider_code in faults)


def _code_for(status: int, provider_code: str) -> ErrorCode:
    if status == 429:  # noqa: PLR2004
        # Out of quota is a billing problem on our key, not throttling (rule 1).
        quota = provider_code in {"insufficient_quota", "billing_hard_limit_reached"}
        return ErrorCode.UPSTREAM_UNAVAILABLE if quota else ErrorCode.UPSTREAM_THROTTLED
    if provider_code in _CONTEXT_CODES or status == 413:  # noqa: PLR2004
        return ErrorCode.REQUEST_TOO_LARGE
    if status in (400, 409, 422):
        return ErrorCode.INVALID_REQUEST
    # 401/403/404 mean our key, model or deployment is wrong; 408 and 5xx are the provider's.
    return ErrorCode.UPSTREAM_UNAVAILABLE
