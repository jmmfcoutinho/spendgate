"""Azure Speech fast transcription: the request it receives, the answer it gives, its errors.

Reference (Microsoft Learn, "Fast transcription"):
`POST {base}/speechtotext/transcriptions:transcribe?api-version=2025-10-15`; multipart parts
`audio` and `definition` (JSON); answer `{durationMilliseconds, combinedPhrases[{text}],
phrases[{offsetMilliseconds, durationMilliseconds, text, locale, words[]}]}`. Pure functions; the
adapter in `azure_speech` does the wiring.
"""

import json
import math
import re
import secrets
from collections.abc import Mapping
from decimal import Decimal

from spendgate.core.audio import AudioError, measure
from spendgate.core.errors import ErrorCode
from spendgate.providers.port import (
    Json,
    Untranslatable,
    UpstreamError,
    UpstreamResponse,
    load_json,
    retry_after_seconds,
)

API_VERSION = "2025-10-15"
PATH = "/speechtotext/transcriptions:transcribe"
MAI_MODEL = "mai-transcribe-2"
MAI_AZURE_NAME = "MAI-Transcribe-2"  # Microsoft Learn: enhancedMode.model
SCOPE = "https://cognitiveservices.azure.com/.default"  # Entra token audience for Speech
VERSION_FORMAT = re.compile(r"[0-9A-Za-z.\-]{1,32}")  # an api-version cannot smuggle a query
# OpenAI sends an ISO 639-1 code; Azure wants a locale. A closed table: an unknown language is
# refused, never guessed (a wrong locale transcribes badly and bills the same).
LOCALES = {
    "en": "en-US", "pt": "pt-PT", "es": "es-ES", "fr": "fr-FR", "de": "de-DE", "it": "it-IT",
    "nl": "nl-NL", "pl": "pl-PL", "sv": "sv-SE", "da": "da-DK", "fi": "fi-FI", "ja": "ja-JP",
    "ko": "ko-KR", "zh": "zh-CN", "ru": "ru-RU", "tr": "tr-TR", "ar": "ar-SA", "hi": "hi-IN",
}  # fmt: skip
# Fields Azure has no equivalent for. Refused, never silently dropped.
UNSUPPORTED = ("prompt", "temperature")
FORMATS = frozenset({"json", "text", "verbose_json"})  # srt and vtt are not built here
_MAX_DURATION_MS = 10**9  # ~277 hours, beyond any sane answer
_MAX_MESSAGE = 200
_QUOTA = ("resource exhausted", "quota")
_NOT_ENABLED = ("not supported", "enhanced mode")


def check(body: Mapping[str, Json], files: Mapping[str, bytes]) -> bytes:
    """Refuse what Azure cannot carry; return the audio. Raises `Untranslatable`."""
    for key in UNSUPPORTED:
        if key in body:
            raise Untranslatable(f"{key} is not supported by Azure Speech", key)
    fmt = body.get("response_format", "json")
    if fmt not in FORMATS:
        raise Untranslatable(
            "response_format must be json, text or verbose_json", "response_format"
        )
    language = body.get("language")
    if language is not None and language not in LOCALES:
        raise Untranslatable("language is not supported by Azure Speech", "language")
    audio = files.get("file", b"")
    try:
        measure(audio)  # price is the measured duration: unmeasurable audio is refused up front
    except AudioError as exc:
        raise Untranslatable(str(exc), "file") from exc
    return audio


def definition(model: str, language: Json) -> str:
    """The `definition` JSON the gateway builds; callers never choose Azure options."""
    out: dict[str, Json] = {}
    locale = LOCALES.get(language) if isinstance(language, str) else None
    if model == MAI_MODEL:
        out["enhancedMode"] = {"enabled": True, "model": MAI_AZURE_NAME}
        if locale:  # MAI takes a bare language code, not a region locale
            out["locales"] = [locale.split("-")[0]]
    else:
        out["profanityFilterMode"] = "None"  # CV words must not be masked
        if locale:
            out["locales"] = [locale]
    return json.dumps(out, separators=(",", ":"))


def multipart(definition_json: str, audio: bytes) -> tuple[str, bytes]:
    """Content type and body. The file extension comes from the bytes, never from the caller."""
    container = measure(audio).container
    boundary = secrets.token_hex(16)
    head = f"--{boundary}\r\nContent-Disposition: form-data; name="
    parts = [
        f'{head}"definition"\r\n\r\n{definition_json}\r\n'.encode(),
        f'{head}"audio"; filename="audio.{container}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n".encode()
        + audio
        + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def _loads(body: bytes) -> Mapping[str, Json] | None:
    data = load_json(body)
    return data if isinstance(data, Mapping) else None


def reported_ms(body: bytes) -> int | None:
    """`durationMilliseconds` of a 2xx answer, rounded up; None when absent or not plausible."""
    value = (_loads(body) or {}).get("durationMilliseconds")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or not 0 <= value < _MAX_DURATION_MS:
        return None
    return math.ceil(value)


def seconds(milliseconds: int) -> Decimal:
    return Decimal(milliseconds) / 1000


# --- answer in the OpenAI shape ------------------------------------------------------------------


def _text(data: Mapping[str, Json]) -> str | None:
    phrases = data.get("combinedPhrases")
    if not isinstance(phrases, list):
        return None
    texts = [p.get("text") for p in phrases if isinstance(p, Mapping)]
    return " ".join(t for t in texts if isinstance(t, str))


def _num(value: Json) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def _objects(value: Json) -> list[Mapping[str, Json]]:
    return [v for v in value if isinstance(v, Mapping)] if isinstance(value, list) else []


def _span(item: Mapping[str, Json]) -> tuple[float, float]:
    start = _num(item.get("offsetMilliseconds")) / 1000
    return start, start + _num(item.get("durationMilliseconds")) / 1000


def _verbose(data: Mapping[str, Json], text: str, body: Mapping[str, Json]) -> dict[str, Json]:
    phrases = _objects(data.get("phrases"))
    segments: list[Json] = []
    words: list[Json] = []
    for i, phrase in enumerate(phrases):
        start, end = _span(phrase)
        segments.append({"id": i, "start": start, "end": end, "text": phrase.get("text", "")})
        for word in _objects(phrase.get("words")):
            w_start, w_end = _span(word)
            words.append({"word": word.get("text", ""), "start": w_start, "end": w_end})
    first = phrases[0].get("locale") if phrases else None
    out: dict[str, Json] = {
        "task": "transcribe",
        "language": first if isinstance(first, str) else body.get("language", ""),
        "duration": _num(data.get("durationMilliseconds")) / 1000,
        "text": text,
        "segments": segments,
    }
    wanted = body.get("timestamp_granularities[]")
    if wanted == "word" or (isinstance(wanted, list) and "word" in wanted):
        out["words"] = words
    return out


def convert(body: Mapping[str, Json], resp: UpstreamResponse) -> UpstreamResponse:
    """A 2xx answer in the OpenAI transcription shape; an unreadable one becomes a bodiless 502."""
    data = _loads(resp.body)
    text = _text(data) if data is not None else None
    if data is None or text is None:
        return UpstreamResponse(502, {}, b"")
    fmt = body.get("response_format", "json")
    if fmt == "text":
        return UpstreamResponse(
            resp.status, {"Content-Type": "text/plain; charset=utf-8"}, text.encode()
        )
    payload: dict[str, Json] = (
        _verbose(data, text, body) if fmt == "verbose_json" else {"text": text}
    )
    out = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return UpstreamResponse(resp.status, {"Content-Type": "application/json"}, out)


# --- errors --------------------------------------------------------------------------------------


def _message(body: bytes) -> str:
    data = _loads(body) or {}
    inner = data.get("error")
    source = inner if isinstance(inner, Mapping) else data
    text = source.get("message")
    return text[:_MAX_MESSAGE] if isinstance(text, str) else ""


def map_error(resp: UpstreamResponse) -> UpstreamError:
    """Core error for an Azure Speech failure (protocol.md section 4.2, rule 1).

    Azure Speech gives no code that tells a missing resource from an answer to this request, so
    no answer is marked a deployment fault: the breaker counts its 401 by status (ADR-0033).
    """
    status = resp.status
    text = _message(resp.body)
    lowered = text.lower()
    if status == 429:  # noqa: PLR2004
        # "Resource exhausted" means no Speech quota on our resource: ours to fix, not throttling.
        code = (
            ErrorCode.UPSTREAM_UNAVAILABLE
            if any(w in lowered for w in _QUOTA)
            else ErrorCode.UPSTREAM_THROTTLED
        )
        return UpstreamError(code, True, status, retry_after_seconds(resp.headers), text)
    if status == 413:  # noqa: PLR2004
        return UpstreamError(ErrorCode.REQUEST_TOO_LARGE, True, status, None, text)
    if status in (400, 422) and not all(w in lowered for w in _NOT_ENABLED):
        return UpstreamError(ErrorCode.INVALID_REQUEST, True, status, None, text)
    # 401/403/404, a model our resource lacks, 408 and 5xx: our key, resource or the provider.
    return UpstreamError(ErrorCode.UPSTREAM_UNAVAILABLE, True, status, None, text)
