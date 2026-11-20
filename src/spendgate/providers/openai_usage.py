"""OpenAI response side, shared with Azure OpenAI: usage readers, stream collector, errors.

Usage is normalised to `core.cost.Usage`: text counters keep their cached and reasoning subsets,
audio tokens move to the separate audio counters so nothing is billed twice. Anything that does
not parse cleanly is "no usage" (None), which the pipeline settles as the full hold.
"""

import math
from collections.abc import Mapping
from decimal import Decimal

from spendgate.core.cost import Usage
from spendgate.core.errors import ErrorCode
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
_MAX_MESSAGE = 500


def _count(obj: Mapping[str, Json], key: str, *, required: bool = False) -> int:
    value = obj.get(key)
    if value is None and not required:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= INT64_MAX:
        raise ValueError(key)
    return value


def _details(usage: Mapping[str, Json], *keys: str) -> Mapping[str, Json]:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _text_usage(u: Mapping[str, Json], in_key: str, out_key: str) -> Usage:
    """Chat (`prompt_tokens`/`completion_tokens`) and Responses (`input_tokens`/...) usage."""
    inp = _details(u, "prompt_tokens_details", "input_tokens_details", "input_token_details")
    out = _details(u, "completion_tokens_details", "output_tokens_details")
    audio_in, audio_out = _count(inp, "audio_tokens"), _count(out, "audio_tokens")
    return Usage(
        input_tokens=max(_count(u, in_key, required=True) - audio_in, 0),
        output_tokens=max(_count(u, out_key) - audio_out, 0),
        cached_input_tokens=_count(inp, "cached_tokens"),
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


def parse_usage(value: Json) -> Usage | None:
    """Read any OpenAI usage object: chat, embeddings, Responses, or transcription (both kinds)."""
    if not isinstance(value, Mapping):
        return None
    try:
        if value.get("type") == "duration":
            return _duration(value)
        if "prompt_tokens" in value:  # chat; embeddings carry no completion_tokens
            return _text_usage(value, "prompt_tokens", "completion_tokens")
        if "input_tokens" in value:  # Responses; transcription with type "tokens"
            return _text_usage(value, "input_tokens", "output_tokens")
    except ValueError:
        return None
    return None


def usage_of(response: UpstreamResponse) -> Usage | None:
    """Usage of a non-streamed answer. Speech is audio bytes, so it has none (None)."""
    body = load_json(response.body)
    return parse_usage(body.get("usage")) if isinstance(body, Mapping) else None


class OpenAIStreamUsage:
    """Keeps the last usage seen: chat's usage-only chunk, or Responses' terminal event."""

    def __init__(self) -> None:
        self._usage: Usage | None = None

    def feed(self, event: SseEvent) -> None:
        obj = load_json(event.data)  # "[DONE]" and malformed events simply carry nothing
        if not isinstance(obj, Mapping):
            return
        holder = obj.get("response") if isinstance(obj.get("response"), Mapping) else obj
        if isinstance(holder, Mapping) and (found := parse_usage(holder.get("usage"))) is not None:
            self._usage = found

    def result(self) -> Usage | None:
        return self._usage


def is_usage_only_chunk(event: SseEvent) -> bool:
    """The final chat chunk that carries usage and no choices."""
    obj = load_json(event.data)
    return isinstance(obj, Mapping) and obj.get("choices") == [] and bool(obj.get("usage"))


# --- errors --------------------------------------------------------------------------------------


def map_error(response: UpstreamResponse) -> UpstreamError:
    """Core error for an OpenAI error answer (protocol.md section 4.2, rule 1)."""
    status = response.status
    body = load_json(response.body)
    err = body.get("error") if isinstance(body, Mapping) else None
    err = err if isinstance(err, Mapping) else {}
    code, text, param = err.get("code"), err.get("message"), err.get("param")
    message = text[:_MAX_MESSAGE] if isinstance(text, str) else ""
    mapped = _code_for(status, code if isinstance(code, str) else "")
    retry = retry_after_seconds(response.headers) if status == 429 else None  # noqa: PLR2004
    del param  # provider param names are not ours; the pipeline reports its own
    return UpstreamError(mapped, True, status, retry, message)


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
