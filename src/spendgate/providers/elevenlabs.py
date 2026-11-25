"""ElevenLabs adapter: OpenAI `/v1/audio/speech` over `POST /v1/text-to-speech/{voice_id}`.

Text-to-speech is billed by characters. The charge is the larger of the input's length and the
`character-cost` header ElevenLabs reports, so a credit multiplier can never under-bill. The URL is
the configured base plus a fixed path and a voice id that matched a strict pattern (SSRF, path
tricks); the body is rebuilt from validated fields, never forwarded. `language_code` is the one
field beyond the OpenAI speech shape (ADR-0026); the shared allowlist refuses it for the others.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus

from spendgate.core.cost import Usage
from spendgate.core.errors import ErrorCode, Refusal
from spendgate.providers import openai_shapes as openai
from spendgate.providers.port import (
    CleanRequest,
    Deployment,
    Json,
    SseLimits,
    StreamRelay,
    Untranslatable,
    UpstreamError,
    UpstreamRequest,
    UpstreamResponse,
    load_json,
    never_raises_error,
    never_raises_response,
    never_raises_usage,
    retry_after_seconds,
)

VOICE_ID = re.compile(
    r"[A-Za-z0-9]{10,40}"
)  # ElevenLabs ids are 20 alphanumerics; OpenAI names fail
COST_HEADER = "character-cost"
MIN_SPEED, MAX_SPEED = 0.7, 1.2  # ElevenLabs voice_settings.speed range
LANGUAGE_CODE = re.compile(r"[a-z]{2}")  # ISO 639-1; a model that cannot use it ignores it
_MAX_MESSAGE = 200
_QUOTA_CODES = frozenset({"quota_exceeded", "insufficient_credits", "payment_required"})
_VOICE_CODES = frozenset({"voice_not_found", "invalid_uid", "voice_not_fine_tuned"})


@dataclass(frozen=True, slots=True)
class _Format:
    output: str  # ElevenLabs output_format
    content_type: str


# OpenAI response_format -> ElevenLabs output_format. aac and flac have no ElevenLabs equivalent;
# wav and pcm use 24 kHz, which needs no paid tier (44.1 kHz does) and is OpenAI's pcm rate.
FORMATS = {
    "mp3": _Format("mp3_44100_128", "audio/mpeg"),
    "opus": _Format("opus_48000_64", "audio/opus"),
    "wav": _Format("wav_24000", "audio/wav"),
    "pcm": _Format("pcm_24000", "audio/pcm"),
}


def _format(body: Mapping[str, Json]) -> _Format:
    name = body.get("response_format", "mp3")
    found = FORMATS.get(name) if isinstance(name, str) else None
    if found is None:
        raise Untranslatable("response_format is not available from ElevenLabs", "response_format")
    return found


def _language(body: Mapping[str, Json]) -> str | None:
    """The ISO 639-1 `language_code` to forward; None when absent or null (ElevenLabs infers it)."""
    code = body.get("language_code")
    if code is None:
        return None
    if not isinstance(code, str) or not LANGUAGE_CODE.fullmatch(code):
        raise Untranslatable("language_code must be an ISO 639-1 code", "language_code")
    return code


def _check(req: CleanRequest) -> tuple[_Format, str, str]:
    """(format, voice id, text) of a request ElevenLabs can serve, else `Untranslatable`."""
    if req.endpoint != openai.SPEECH:
        raise Untranslatable("endpoint is not served by elevenlabs", "endpoint")
    if req.stream:
        raise Untranslatable("speech does not stream", "stream")
    _language(req.body)
    shared = {k: v for k, v in req.body.items() if k != "language_code"}  # ours, checked above
    openai.check(req.endpoint, shared, req.files)  # the shared allowlist and input length
    if "instructions" in req.body:
        raise Untranslatable("instructions are not supported by ElevenLabs", "instructions")
    speed = req.body.get("speed")
    if speed is not None and (
        isinstance(speed, bool)
        or not isinstance(speed, int | float)
        or not MIN_SPEED <= speed <= MAX_SPEED
    ):
        raise Untranslatable(f"speed must be {MIN_SPEED} to {MAX_SPEED} for ElevenLabs", "speed")
    voice, text = req.body.get("voice"), req.body.get("input")
    if not isinstance(voice, str) or not VOICE_ID.fullmatch(voice):
        raise Untranslatable("voice must be an ElevenLabs voice id", "voice")
    if not isinstance(text, str) or not text.strip():
        raise Untranslatable("input must contain text", "input")
    return _format(req.body), voice, text


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return next((v for k, v in headers.items() if k.lower() == name), None)


class ElevenLabs:
    name = "elevenlabs"

    def validate(self, req: CleanRequest) -> Refusal | None:
        try:
            _check(req)
        except Untranslatable as refused:
            return refused.refusal
        return None

    async def prepare(self, deployment: Deployment) -> None:
        """A static API key: nothing to refresh."""

    def translate(self, req: CleanRequest, deployment: Deployment) -> UpstreamRequest:
        """Raises `Untranslatable` for a request ElevenLabs cannot carry."""
        fmt, voice, text = _check(req)
        if not deployment.credential:
            raise ValueError("the deployment has no credential")  # misconfiguration, fail closed
        payload: dict[str, Json] = {"model_id": deployment.model, "text": text}
        speed = req.body.get("speed")
        if speed is not None:
            payload["voice_settings"] = {"speed": speed}
        if (language := _language(req.body)) is not None:
            payload["language_code"] = language
        base = deployment.base_url.rstrip("/").removesuffix("/v1")
        url = f"{base}/v1/text-to-speech/{voice}?output_format={fmt.output}"
        headers = {
            "xi-api-key": deployment.credential,
            "Content-Type": "application/json",
            "Accept": fmt.content_type,
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        return UpstreamRequest("POST", url, headers, body)

    @never_raises_response
    def response(self, req: CleanRequest, resp: UpstreamResponse) -> UpstreamResponse:
        """Raw audio bytes with the requested format's content type (the OpenAI speech shape)."""
        if not HTTPStatus.OK <= resp.status < HTTPStatus.MULTIPLE_CHOICES:
            return resp
        if not resp.body:
            return UpstreamResponse(502, {}, b"")  # a 2xx without audio is unreadable
        return UpstreamResponse(
            resp.status, {"Content-Type": _format(req.body).content_type}, resp.body
        )

    def relay(self, req: CleanRequest, limits: SseLimits = SseLimits()) -> StreamRelay:  # noqa: B008
        raise Untranslatable("speech does not stream", "stream")

    @never_raises_usage
    def usage(self, req: CleanRequest, response: UpstreamResponse) -> Usage | None:
        """Billed characters: the input's length, raised to ElevenLabs' own count when higher."""
        if not HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES:
            return None
        text = req.body.get("input")
        if not isinstance(text, str):
            return None
        reported = _header(response.headers, COST_HEADER)
        extra = _reported_characters(reported)
        return Usage(characters=max(len(text), extra))

    @never_raises_error
    def error(self, response: UpstreamResponse) -> UpstreamError:
        return _map_error(response)


_MAX_DIGITS = 15  # keeps the count far below int64


def _reported_characters(raw: str | None) -> int:
    """ElevenLabs' billed-character count; 0 (the input length stands) unless plain ASCII digits."""
    text = (raw or "").strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) <= _MAX_DIGITS else 0


def _detail(body: bytes) -> tuple[str, str]:
    """(code, message) from `{"detail": {"status", "message"}}`; empty strings when absent."""
    data = load_json(body)
    detail = data.get("detail") if isinstance(data, Mapping) else None
    if not isinstance(detail, Mapping):
        return "", ""
    code = next(
        (detail[k] for k in ("status", "code", "type") if isinstance(detail.get(k), str)), ""
    )
    message = detail.get("message")
    return str(code), message[:_MAX_MESSAGE] if isinstance(message, str) else ""


def _map_error(resp: UpstreamResponse) -> UpstreamError:
    """Core error for an ElevenLabs failure (protocol.md section 4.2, rule 1)."""
    status = resp.status
    code, message = _detail(resp.body)
    if code in _QUOTA_CODES:  # our account is out of credits: ours to fix, whatever the status
        mapped = ErrorCode.UPSTREAM_UNAVAILABLE
    elif status == HTTPStatus.TOO_MANY_REQUESTS:
        mapped = ErrorCode.UPSTREAM_THROTTLED
    elif status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE:
        mapped = ErrorCode.REQUEST_TOO_LARGE
    elif (
        status in (HTTPStatus.BAD_REQUEST, HTTPStatus.UNPROCESSABLE_ENTITY) or code in _VOICE_CODES
    ):
        mapped = ErrorCode.INVALID_REQUEST
    else:  # 401/402/403 are our key or plan; 5xx and the rest are the provider's
        mapped = ErrorCode.UPSTREAM_UNAVAILABLE
    retry = retry_after_seconds(resp.headers) if status == HTTPStatus.TOO_MANY_REQUESTS else None
    return UpstreamError(mapped, True, status, retry, message)
