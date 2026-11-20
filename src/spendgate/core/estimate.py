"""Worst-case request estimates: what a call may consume before it is sent.

The hold is sized from this, so every rule errs high. Nothing here trusts the caller: audio length
is measured from the bytes, images are counted per part, and the output ceiling is resolved from
the request, the key and the catalog (protocol.md section 2.1). Pure: no I/O, no clock.

Text input is counted by an injected `tokenizer` (the optional `tokenizer` extra wires tiktoken in
the caller) or, without one, by `heuristic_tokens`. Both get the per-message overhead and the
`safety` percentage, because the provider's own tokenizer can differ from the one we hold.
"""

import base64
import binascii
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum

from spendgate.core.audio import AudioError, measure
from spendgate.core.catalog import PriceEntry
from spendgate.core.cost import Estimate
from spendgate.core.errors import ErrorCode, Refusal

type Tokenizer = Callable[[str], int]
type Body = Mapping[str, object]


class RequestKind(StrEnum):
    """Values equal the catalog `mode` of the entry that prices the request."""

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
# Per image part, whatever its size: the ceiling of patch-based vision billing (ported from an
# earlier guard). The catalog only has a money meter for images (`per_image`), no token count.
IMAGE_TOKENS = 1_536
# Audio as tokens, for token-billed audio input: above the highest documented rate (32/s).
AUDIO_TOKENS_PER_SECOND = 50

_CHARS_PER_TOKEN = 3
_LONG_RUN = 12  # an unbroken alphanumeric run longer than this is an identifier or a hash
_WS_PER_TOKEN = 4  # indentation
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
_RUNS = re.compile(r"[A-Za-z0-9\u00c0-\u024f]+|\s+|.", re.DOTALL)


class _Invalid(Exception):
    def __init__(self, param: str, message: str) -> None:
        super().__init__(message)
        self.refusal = Refusal(ErrorCode.INVALID_REQUEST, message, param=param)


_ASCII_END = 0x80
_TWO_BYTE_END = 0x800
_BMP_END = 0x10000
_LATIN_END = 0x250  # Latin-1 supplement and Latin Extended-A/B: letters that merge into words
# CJK ideographs, kana, hangul: tokenizers spend about one or two tokens on each.
_CJK_RANGES = ((0x3000, 0x9FFF), (0xAC00, 0xD7AF), (0xF900, 0xFAFF))


def _is_cjk(code: int) -> bool:
    return any(low <= code <= high for low, high in _CJK_RANGES)


def _symbol_tokens(chars: str) -> int:
    """Per-character caps: a code point never costs more tokens than its UTF-8 bytes."""
    total = 0
    for char in chars:
        code = ord(char)
        if code < _ASCII_END:
            total += 1
        elif code >= _BMP_END:
            total += 4
        elif _is_cjk(code):
            total += 2
        else:
            total += 3 if code >= _TWO_BYTE_END else 2
    return total


def heuristic_tokens(text: str) -> int:
    """Upper-bound token count without a tokenizer.

    Alphanumeric runs cost one token per 3 characters (at least one per word); unbroken runs over
    12 characters (hashes, base64) three quarters of a token per character; a single space nothing
    (it rides on the next word); indentation one token per 4; everything else the byte-length cap
    of `_symbol_tokens`. Measured against tiktoken in tests/unit/test_estimate.py.
    """
    total = 0
    for match in _RUNS.finditer(text):
        run = match.group()
        length = len(run)
        if run[0].isalnum() and ord(run[0]) < _LATIN_END:
            total += -(-length // _CHARS_PER_TOKEN) if length <= _LONG_RUN else -(-length * 3 // 4)
        elif run == " ":
            continue
        elif length > 1 and run.isascii() and run.isspace():
            total += -(-length // _WS_PER_TOKEN)
        else:
            total += _symbol_tokens(run)
    return total


@dataclass(slots=True)
class _Input:
    """What the walk found: text to count plus the things that are not text."""

    texts: list[str] = field(default_factory=list)
    messages: int = 0
    images: int = 0
    fixed_tokens: int = 0  # token ids and numbers: already exact or worst-case, no margin
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


def _walk(value: object, acc: _Input, param: str, depth: int = 0) -> None:
    """Add every billable leaf below `value`: strings, keys, numbers, images, audio."""
    if depth > _MAX_DEPTH:
        raise _Invalid(param, f"{param} is nested too deeply")
    if isinstance(value, str):
        acc.texts.append(value)
    elif isinstance(value, Mapping):
        part_type = value.get("type")
        if part_type in _IMAGE_PARTS:
            acc.images += 1  # the pixels are not text: never walk a data URL
        elif part_type == "input_audio":
            _audio_part(value, acc, param)
        else:
            for key, item in value.items():
                if isinstance(key, str):
                    acc.texts.append(key)
                _walk(item, acc, param, depth + 1)
    elif isinstance(value, list | tuple):
        for item in value:
            _walk(item, acc, param, depth + 1)
    elif value is not None:
        acc.fixed_tokens += 1  # a number or boolean serialises to at least one token


def _collect_text_kind(body: Body, acc: _Input) -> None:
    for key, value in body.items():
        if key in _IGNORED:
            continue
        if key in _STRUCTURED:
            try:
                acc.texts.append(json.dumps(value, separators=(",", ":"), ensure_ascii=False))
            except (TypeError, ValueError) as exc:
                raise _Invalid(key, f"{key} is not valid JSON") from exc
            continue
        if key in ("messages", "input") and isinstance(value, list):
            acc.messages += len(value)
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
    """
    try:
        return _resolve_ceiling(kind, body, entry, key_max_output_tokens)
    except _Invalid as exc:
        return exc.refusal


def _resolve_ceiling(
    kind: RequestKind, body: Body, entry: PriceEntry, key_cap: int | None
) -> int | Refusal:
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


def _input_tokens(acc: _Input, tokenizer: Tokenizer | None, safety: int) -> int:
    counted = sum(_count_text(t, tokenizer) for t in acc.texts)
    text = counted + REQUEST_OVERHEAD_TOKENS + MESSAGE_OVERHEAD_TOKENS * acc.messages
    # Only counted text takes the margin; images and token ids are already worst-case or exact.
    return -(-text * (100 + safety) // 100) + acc.fixed_tokens + IMAGE_TOKENS * acc.images


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


def estimate(  # noqa: PLR0913 - keyword-only knobs of one pure function
    kind: RequestKind,
    body: Body,
    *,
    entry: PriceEntry,
    key_max_output_tokens: int | None = None,
    tokenizer: Tokenizer | None = None,
    safety: int = DEFAULT_SAFETY_PERCENT,
) -> Estimate | Refusal:
    """Upper bounds for one request, or the `Refusal` that stops it before any hold.

    `body` is the parsed, allowlist-validated request (transcriptions: the `file` bytes and the
    form fields as a mapping). Meters other than text tokens are set only when the entry prices
    them, so `worst_case` cannot fail on a count added out of caution. Images are always counted as
    `IMAGE_TOKENS` input tokens and also per image when the entry has `per_image`. Audio length is
    measured from the bytes, never read from the request, and also expressed as tokens at
    `AUDIO_TOKENS_PER_SECOND` when the entry has a token meter for audio input; `worst_case` then
    reserves the dearer of the minute and token prices.
    """
    if isinstance(safety, bool) or not isinstance(safety, int) or safety < 0:
        raise ValueError("safety must be a non-negative integer percentage")
    if key_max_output_tokens is not None and key_max_output_tokens <= 0:
        raise ValueError("key_max_output_tokens must be positive")
    ceiling = resolve_output_ceiling(
        kind, body, entry=entry, key_max_output_tokens=key_max_output_tokens
    )
    if isinstance(ceiling, Refusal):
        return ceiling
    acc = _Input()
    try:
        characters = _collect(kind, body, acc)
    except _Invalid as exc:
        return exc.refusal
    tokens = _input_tokens(acc, tokenizer, safety)
    if kind in (RequestKind.SPEECH, RequestKind.TRANSCRIPTION) and not _priced(
        entry, "input_per_mtok"
    ):
        tokens = 0  # billed by characters or audio, not by the text that rides along
    audio_tokens = 0
    if _priced(entry, "audio_input_per_mtok", "cached_audio_input_per_mtok"):
        audio_tokens = _audio_tokens(acc.audio_seconds)
    return Estimate(
        input_tokens=tokens,
        max_output_tokens=ceiling,
        audio_input_tokens=audio_tokens,
        audio_seconds=acc.audio_seconds,
        characters=characters if _priced(entry, "per_1k_characters") else 0,
        images=acc.images if _priced(entry, "per_image") else 0,
    )
