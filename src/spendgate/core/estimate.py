"""Worst-case request estimates: what a call may consume before it is sent.

The hold is sized from this, so every rule errs high. Nothing here trusts the caller: audio length
is measured from the bytes, images are counted per part, and the output ceiling is resolved from
the request, the key and the catalog (protocol.md section 2.1). Pure: no I/O, no clock.

Text input is counted by an injected `tokenizer` (the optional `tokenizer` extra wires tiktoken in
the caller) or, without one, by `heuristic_tokens` (core/heuristic.py). Both get the per-message
overhead and the `safety` percentage, because the provider's own tokenizer can differ from the one
we hold. Neither is a proof, so the estimate also carries a hard bound, one token per UTF-8 byte,
from which `worst_case` chooses a long-context tier (M-14).
"""

import base64
import binascii
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum

from spendgate.core.audio import AudioError, measure
from spendgate.core.catalog import OUTPUT_METERS, PriceEntry
from spendgate.core.cost import Estimate
from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.heuristic import heuristic_tokens, utf8_bound

type Tokenizer = Callable[[str], int]
type Body = Mapping[str, object]


class RequestKind(StrEnum):
    """An endpoint's kind. Values are catalog modes; `core.modes.serves` says which entries fit."""

    CHAT = "chat"
    RESPONSES = "responses"
    MESSAGES = "messages"
    EMBEDDING = "embedding"
    TRANSCRIPTION = "transcription"
    SPEECH = "speech"


# Added to the text count before the safety factor. Chat formats cost a few tokens per message
# (role, separators) and a few per request (reply priming); 8 and 16 sit above published figures.
MESSAGE_OVERHEAD_TOKENS = 8
REQUEST_OVERHEAD_TOKENS = 16
# Extra percentage on the counted text; 25 matches the ledger's default safety factor (1.25).
DEFAULT_SAFETY_PERCENT = 25
# Per image part, whatever its size, when the entry sets no `limits.max_image_tokens`: the largest
# per-image cost any provider documents, plus 25 percent. The OpenAI vision guide (read 2026-11-22)
# lists up to 48,169 tokens for one image (gpt-4o-mini, high detail; gpt-6-astra "original" is
# 36,000). An entry may only lower it, with a cited source. Only images reach the estimate (M-14).
DEFAULT_IMAGE_TOKENS = 61_000
# Audio as tokens, for token-billed audio input: above the highest documented rate (32/s).
AUDIO_TOKENS_PER_SECOND = 50

# Providers render tool definitions into a hidden system prompt around the JSON (counted as text
# below): Anthropic publishes up to 530 tokens per request for it, OpenAI wraps each function in a
# namespace. Added, like the overheads above, before the safety factor, for every provider.
TOOLS_OVERHEAD_TOKENS = 600
TOOL_OVERHEAD_TOKENS = 32  # per definition
# A JSON-schema or JSON-mode output format is also turned into hidden instructions around the
# schema (counted as text below); assumed to fit the same budget as the tool prompt.
STRUCTURED_OUTPUT_OVERHEAD_TOKENS = 600

_MAX_DEPTH = 100
_ZERO = Decimal(0)

# Request fields that carry the output ceiling, per kind.
_CEILING_FIELDS: Mapping[RequestKind, tuple[str, ...]] = {
    RequestKind.CHAT: ("max_tokens", "max_completion_tokens"),
    RequestKind.RESPONSES: ("max_output_tokens",),
    RequestKind.MESSAGES: ("max_tokens",),
}
# Top-level fields that are configuration, not input.
_IGNORED = frozenset(
    {"model", "stream", "store", "max_tokens", "max_completion_tokens", "max_output_tokens"}
)
# Billed as input text in their JSON form (schemas, tool definitions, output formats).
_STRUCTURED = frozenset({"tools", "tool_choice", "response_format", "text", "output_config"})
_IMAGE_PARTS = frozenset({"image_url", "input_image", "image"})
# Fields that hold content parts: a message's or item's `content`, a Responses
# `function_call_output.output`, an Anthropic `tool_result.content`.
_PART_LISTS = ("content", "output")
# Encrypted or signed fields the validators accept, per part type: opaque to any tokenizer, so
# counted at their bytes (the plaintext's tokens are at most its bytes, at most the ciphertext's).
_OPAQUE = {"thinking": "signature", "redacted_thinking": "data", "reasoning": "encrypted_content"}


class _Invalid(Exception):
    def __init__(self, param: str, message: str) -> None:
        super().__init__(message)
        self.refusal = Refusal(ErrorCode.INVALID_REQUEST, message, param=param)


@dataclass(slots=True)
class _Input:
    """What the walk found: text to count plus the things that are not text."""

    texts: list[str] = field(default_factory=list)
    extra_bound: int = 0  # bytes the hard bound adds beyond the texts (JSON's widest rendering)
    structured_output: bool = False
    messages: int = 0
    tools: int = 0  # tool definitions, each rendered into the hidden prompt
    images: int = 0
    fixed_tokens: int = 0  # embedding token ids, opaque blobs at their bytes: hard, no margin
    audio_seconds: Decimal = _ZERO


def _measure_audio(raw: bytes, param: str) -> Decimal:
    try:
        return Decimal(measure(raw).duration_ms) / 1000
    except AudioError as exc:
        raise _Invalid(param, f"{param} is not audio of a measurable length: {exc}") from exc
    except ArithmeticError as exc:  # a duration too large to represent (SR-07)
        raise _Invalid(param, f"{param} is not audio of a measurable length") from exc


def _audio_part(part: Mapping[str, object], acc: _Input, param: str) -> None:
    inner = part.get("input_audio")
    data = inner.get("data") if isinstance(inner, Mapping) else None
    if not isinstance(data, str):
        raise _Invalid(param, "input_audio needs base64 data")
    try:
        raw = base64.b64decode(data, validate=True)
    except binascii.Error as exc:
        raise _Invalid(param, "input_audio data is not valid base64") from exc
    acc.audio_seconds += _measure_audio(raw, param)


def _scalar_text(value: object) -> str:
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


def _json_text(value: object, acc: _Input, param: str) -> None:
    """Free-form JSON, counted as the text the provider renders it to.

    The estimate counts the compact form; the bound takes the bytes of the widest rendering
    (indented, ASCII-escaped), which is at least the bytes of every other one.
    """
    try:
        compact = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        widest = json.dumps(value, indent=2, ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise _Invalid(param, f"{param} is not valid JSON") from exc
    acc.texts.append(compact)
    acc.extra_bound += max(len(widest) - utf8_bound(compact), 0)


def _walk(value: object, acc: _Input, param: str, depth: int = 0, *, part: bool = False) -> None:
    """Add every billable leaf below `value`: strings, keys, numbers, images, audio.

    `part` is true only where the validators accept a content part (an element of a message's
    `content`, a Responses item's `output`, or an Anthropic `tool_result.content`). Elsewhere an
    object that merely says `"type": "image"` (tool input, arguments, citations) is text, so a
    client cannot hide text behind an image label and have it held as one image (M-14).
    """
    if depth > _MAX_DEPTH:
        raise _Invalid(param, f"{param} is nested too deeply")
    if isinstance(value, str):
        acc.texts.append(value)
    elif isinstance(value, Mapping):
        part_type = value.get("type") if part else None
        if part_type in _IMAGE_PARTS:
            acc.images += 1  # the pixels are not text: never walk a data URL
        elif part_type == "input_audio":
            _audio_part(value, acc, param)
        else:
            lists = ("content",) if part_type == "tool_result" else ()
            _walk_fields(value, acc, param, depth, lists)
    elif isinstance(value, list | tuple):
        for item in value:
            _walk(item, acc, param, depth + 1)
    else:  # a number, boolean or null: counted as the text it serialises to
        acc.texts.append(_scalar_text(value))


def _walk_fields(
    obj: Mapping[object, object], acc: _Input, param: str, depth: int, lists: tuple[str, ...]
) -> None:
    """Keys and values of `obj`; the lists named in `lists` hold content parts."""
    obj_type = obj.get("type")
    opaque = _OPAQUE.get(obj_type) if isinstance(obj_type, str) else None
    for key, item in obj.items():
        if isinstance(key, str):
            acc.texts.append(key)
        if key in lists and isinstance(item, list):
            for element in item:
                _walk(element, acc, param, depth + 2, part=True)
        elif key == opaque and isinstance(item, str):
            acc.fixed_tokens += utf8_bound(item)  # ciphertext bytes bound the plaintext's tokens
        elif obj_type == "tool_use" and key == "input":
            _json_text(item, acc, param)  # any JSON, image-shaped or not: its text is billed
        else:
            _walk(item, acc, param, depth + 1)


def _structured_output(key: str, value: object) -> bool:
    """A JSON-schema or JSON-mode output format (chat, Responses and Messages spellings)."""
    if not isinstance(value, Mapping):
        return False
    if key == "output_config":
        return value.get("format") is not None
    fmt = value.get("format") if key == "text" else value
    return isinstance(fmt, Mapping) and fmt.get("type") in ("json_schema", "json_object")


def _collect_text_kind(body: Body, acc: _Input) -> None:
    for key, value in body.items():
        if key in _IGNORED:
            continue
        if key in _STRUCTURED:
            if key == "tools" and isinstance(value, list):
                acc.tools += len(value)
            acc.structured_output |= _structured_output(key, value)
            _json_text(value, acc, key)
            continue
        if key in ("messages", "input") and isinstance(value, list):
            acc.messages += len(value)
            for entry in value:  # a message or input item: its content lists hold the parts
                if isinstance(entry, Mapping):
                    _walk_fields(entry, acc, key, 1, _PART_LISTS)
                else:
                    _walk(entry, acc, key, 1)
            continue
        _walk(value, acc, key)


def _int_param(body: Body, name: str) -> int | None:
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _Invalid(name, f"{name} must be a positive integer")
    return value


def _embedding_input(body: Body, acc: _Input) -> None:
    value = body.get("input")
    if isinstance(value, str):
        items: list[object] = [value]
    elif isinstance(value, list) and value:
        items = list(value)
    else:
        raise _Invalid("input", "input must be a string or a non-empty array")
    for item in items:
        if isinstance(item, str):
            acc.texts.append(item)
        elif isinstance(item, int) and not isinstance(item, bool):
            acc.fixed_tokens += 1  # one id of a flat token array
        elif isinstance(item, list) and all(isinstance(i, int) for i in item):
            acc.fixed_tokens += len(item)
        else:
            raise _Invalid("input", "input items must be strings or token arrays")


def _text_param(body: Body, name: str, acc: _Input, *, required: bool = False) -> int:
    """Add a string field to the text; returns its length in characters."""
    value = body.get(name)
    if value is None and not required:
        return 0
    if not isinstance(value, str) or not value:
        raise _Invalid(name, f"{name} must be a non-empty string")
    acc.texts.append(value)
    return len(value)


def resolve_output_ceiling(
    kind: RequestKind,
    body: Body,
    *,
    entry: PriceEntry,
    key_max_output_tokens: int | None = None,
) -> int | Refusal:
    """The output tokens the hold assumes and the gateway injects (protocol.md section 2.1).

    A request value may lower the ceiling but not raise it above the smaller of the key's and the
    catalog's (`request_too_large`); with no request value the smaller of those two applies; with
    none of the three the request is refused (M-09). Kinds that bill no output tokens return 0.
    A token-billed transcript is held at the catalog's ceiling alone (M-19).
    """
    try:
        return _resolve_ceiling(kind, body, entry, key_max_output_tokens)
    except _Invalid as exc:
        return exc.refusal


def _transcript_ceiling(entry: PriceEntry) -> int | Refusal:
    """0, or the model's own output limit: no transcription field bounds the transcript (M-19).

    So no ceiling can be forwarded, and a key or request value would shrink only the hold.
    """
    if not _priced(entry, *OUTPUT_METERS):
        return 0
    cap = entry.limits.max_output_tokens if entry.limits is not None else None
    if cap is None:
        message = f"{entry.id} bills transcript tokens but sets no limits.max_output_tokens"
        return Refusal(ErrorCode.MODEL_NOT_PRICED, message)
    return cap


def _resolve_ceiling(
    kind: RequestKind, body: Body, entry: PriceEntry, key_cap: int | None
) -> int | Refusal:
    if kind is RequestKind.TRANSCRIPTION:
        return _transcript_ceiling(entry)
    names = _CEILING_FIELDS.get(kind, ())
    speech_tokens = kind is RequestKind.SPEECH and entry.meters.audio_output_per_mtok is not None
    if not names and not speech_tokens:
        return 0
    catalog_cap = entry.limits.max_output_tokens if entry.limits is not None else None
    caps = [c for c in (key_cap, catalog_cap) if c is not None]
    cap = min(caps) if caps else None
    requested = {n: v for n in names if (v := _int_param(body, n)) is not None}
    for name, value in requested.items():
        if cap is not None and value > cap:
            return Refusal(
                ErrorCode.REQUEST_TOO_LARGE,
                f"{name} {value} is above the allowed maximum of {cap}",
                param=name,
                details={"max_output_tokens": cap},
            )
    if requested:
        return min(requested.values())
    if cap is None:
        name = names[0] if names else "max_output_tokens"
        return Refusal(
            ErrorCode.INVALID_REQUEST,
            f"no output ceiling: set {name} (the key and the catalog set none)",
            param=name,
        )
    return cap


def _count_text(text: str, tokenizer: Tokenizer | None) -> int:
    if tokenizer is not None:
        try:
            counted = tokenizer(text)
        except (
            Exception
        ):  # a tokenizer may refuse text (tiktoken: "<|endoftext|>"): fall back, stay safe
            return heuristic_tokens(text)
        if isinstance(counted, int) and not isinstance(counted, bool) and counted >= 0:
            return counted
    return heuristic_tokens(text)


def image_tokens(entry: PriceEntry) -> int:
    """Input tokens held per image part: the entry's ceiling, else the conservative default."""
    ceiling = entry.limits.max_image_tokens if entry.limits is not None else None
    return DEFAULT_IMAGE_TOKENS if ceiling is None else ceiling


def _overhead(acc: _Input) -> int:
    tools = TOOLS_OVERHEAD_TOKENS + TOOL_OVERHEAD_TOKENS * acc.tools if acc.tools else 0
    schema = STRUCTURED_OUTPUT_OVERHEAD_TOKENS if acc.structured_output else 0
    return REQUEST_OVERHEAD_TOKENS + MESSAGE_OVERHEAD_TOKENS * acc.messages + tools + schema


def _input_tokens(acc: _Input, tokenizer: Tokenizer | None, safety: int, per_image: int) -> int:
    text = sum(_count_text(t, tokenizer) for t in acc.texts) + _overhead(acc)
    # Only counted text takes the margin; images and token ids are already worst-case or exact.
    return -(-text * (100 + safety) // 100) + acc.fixed_tokens + per_image * acc.images


def _input_bound(acc: _Input, per_image: int) -> int:
    """A ceiling no tokenizer can exceed: one token per UTF-8 byte of the text (M-14)."""
    text = sum(utf8_bound(t) for t in acc.texts) + acc.extra_bound + _overhead(acc)
    return text + acc.fixed_tokens + per_image * acc.images


def _input_counts(
    acc: _Input, tokenizer: Tokenizer | None, safety: int, per_image: int
) -> tuple[int, int]:
    """The input estimate and its hard bound (see `Estimate`)."""
    return _input_tokens(acc, tokenizer, safety, per_image), _input_bound(acc, per_image)


def _priced(entry: PriceEntry, *meters: str) -> bool:
    return any(getattr(entry.meters, m) is not None for m in meters)


def _collect(kind: RequestKind, body: Body, acc: _Input) -> int:
    """Fill `acc` for the kind; returns the characters to bill (speech only)."""
    if kind is RequestKind.EMBEDDING:
        _embedding_input(body, acc)
    elif kind is RequestKind.TRANSCRIPTION:
        raw = body.get("file")
        if not isinstance(raw, bytes | bytearray) or not raw:
            raise _Invalid("file", "file is required")
        acc.audio_seconds = _measure_audio(bytes(raw), "file")
        _text_param(body, "prompt", acc)
    elif kind is RequestKind.SPEECH:
        characters = _text_param(body, "input", acc, required=True)
        _text_param(body, "instructions", acc)
        return characters
    else:
        _collect_text_kind(body, acc)
    return 0


def _audio_tokens(seconds: Decimal) -> int:
    """Exact Decimal arithmetic, rounded up: a fraction of a token still counts (CR-11)."""
    return int((seconds * AUDIO_TOKENS_PER_SECOND).to_integral_value(ROUND_CEILING))


def _check_knobs(safety: int, key_max_output_tokens: int | None) -> None:
    """Caller bugs, not request faults: raised, never turned into a refusal."""
    if isinstance(safety, bool) or not isinstance(safety, int) or safety < 0:
        raise ValueError("safety must be a non-negative integer percentage")
    if key_max_output_tokens is not None and key_max_output_tokens <= 0:
        raise ValueError("key_max_output_tokens must be positive")


def _held_or_resolved(
    kind: RequestKind, body: Body, entry: PriceEntry, key_cap: int | None, held: int | None
) -> int | Refusal:
    """The ceiling the caller already holds and forwards, else the one the request resolves to."""
    if held is not None:
        return held
    return resolve_output_ceiling(kind, body, entry=entry, key_max_output_tokens=key_cap)


def estimate(  # noqa: PLR0913 - keyword-only knobs of one pure function
    kind: RequestKind,
    body: Body,
    *,
    entry: PriceEntry,
    key_max_output_tokens: int | None = None,
    tokenizer: Tokenizer | None = None,
    safety: int = DEFAULT_SAFETY_PERCENT,
    output_ceiling: int | None = None,
) -> Estimate | Refusal:
    """Upper bounds for one request, or the `Refusal` that stops it before any hold.

    `body` is the parsed, allowlist-validated request (transcriptions: the `file` bytes and the
    form fields as a mapping). Meters other than text tokens are set only when the entry prices
    them, so `worst_case` cannot fail on a count added out of caution. Images are always counted as
    `image_tokens(entry)` input tokens and also per image when the entry has `per_image`. Audio
    length is measured from the bytes, never read from the request, and also expressed as tokens at
    `AUDIO_TOKENS_PER_SECOND` when the entry has a token meter for audio input; `worst_case` then
    reserves the dearer of the minute and token prices. A given `output_ceiling` (the one the
    caller resolved and forwards upstream) is used as is, so hold and request agree (M-15).
    """
    _check_knobs(safety, key_max_output_tokens)
    ceiling = _held_or_resolved(kind, body, entry, key_max_output_tokens, output_ceiling)
    if isinstance(ceiling, Refusal):
        return ceiling
    acc = _Input()
    try:
        characters = _collect(kind, body, acc)
    except _Invalid as exc:
        return exc.refusal
    tokens, bound = _input_counts(acc, tokenizer, safety, image_tokens(entry))
    if kind in (RequestKind.SPEECH, RequestKind.TRANSCRIPTION) and not _priced(
        entry, "input_per_mtok"
    ):
        tokens = bound = 0  # billed by characters or audio, not by the text that rides along
    audio_tokens = 0
    if _priced(entry, "audio_input_per_mtok", "cached_audio_input_per_mtok"):
        audio_tokens = _audio_tokens(acc.audio_seconds)
    return Estimate(
        input_tokens=tokens,
        input_tokens_bound=max(bound, tokens),
        max_output_tokens=ceiling,
        audio_input_tokens=audio_tokens,
        audio_seconds=acc.audio_seconds,
        characters=characters if _priced(entry, "per_1k_characters") else 0,
        images=acc.images if _priced(entry, "per_image") else 0,
    )
