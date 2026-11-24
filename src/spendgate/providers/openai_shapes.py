"""OpenAI wire shapes shared by the OpenAI and Azure OpenAI adapters (same bodies and usage).

Request side: per-endpoint allowlists (protocol.md section 2). `validate` is pure and returns the
refusal the pipeline sends before routing; `rebuild` copies only validated fields and applies the
forced ones, so the raw client body is never forwarded. Response side: usage readers, the stream
usage collector and the error mapping. It imports only `core` and the port: adapters may not
import each other, so what two of them need lives here.
"""

import json
import re
import secrets
from collections.abc import Collection, Mapping
from typing import NoReturn

from spendgate.core.audio import AudioError, measure
from spendgate.core.cost import Usage
from spendgate.core.errors import Refusal
from spendgate.core.media import IMAGE_URL_RULE, inline_image, is_remote
from spendgate.providers.port import CleanRequest, Json, Untranslatable, held_ceiling

type Body = Mapping[str, Json]

CHAT = "/v1/chat/completions"
RESPONSES = "/v1/responses"
EMBEDDINGS = "/v1/embeddings"
TRANSCRIPTIONS = "/v1/audio/transcriptions"
SPEECH = "/v1/audio/speech"
ENDPOINTS = frozenset({CHAT, RESPONSES, EMBEDDINGS, TRANSCRIPTIONS, SPEECH})


def _refuse(param: str, why: str) -> NoReturn:
    # Names the field, never its value: values may be user content.
    raise Untranslatable(f"{param} {why}", param)


# --- allowlists ----------------------------------------------------------------------------------

_COMMON_SAMPLING = ("temperature", "top_p", "user", "stream", "tools", "tool_choice")
_CHAT_KEYS = frozenset(
    {
        "model", "messages", "max_tokens", "max_completion_tokens", "stop", "seed", "n",
        "presence_penalty", "frequency_penalty", "logit_bias", "logprobs", "top_logprobs",
        "response_format", "parallel_tool_calls", "reasoning_effort", "verbosity",
        "stream_options", "store", "modalities", *_COMMON_SAMPLING,
    }
)  # fmt: skip
_RESPONSES_KEYS = frozenset(
    {
        "model", "input", "instructions", "max_output_tokens", "text", "reasoning", "store",
        "parallel_tool_calls", "truncation", "include", "prompt_cache_key", "safety_identifier",
        *_COMMON_SAMPLING,
    }
)  # fmt: skip
_EMBEDDING_KEYS = frozenset({"model", "input", "encoding_format", "dimensions", "user"})
_TRANSCRIPTION_KEYS = frozenset(
    {"model", "language", "prompt", "response_format", "temperature", "timestamp_granularities[]"}
)
_SPEECH_KEYS = frozenset(
    {"model", "input", "voice", "instructions", "response_format", "speed", "stream_format"}
)
# Why a known OpenAI field is refused, for a clearer message than "unknown".
_PRICE_CHANGING = "changes the price and is not supported"
_REFUSED_WHY: Mapping[str, str] = {
    "service_tier": _PRICE_CHANGING,  # flex, priority, scale, fast bill differently from standard
    "prompt_cache_retention": _PRICE_CHANGING,  # extended caching is a separate cache price
    "prediction": _PRICE_CHANGING,  # rejected prediction tokens are billed as output
    "web_search_options": "is a hosted tool and is not supported",
    "previous_response_id": "is server-side state and is not supported",
    "conversation": "is server-side state and is not supported",
    "background": "is server-side state and is not supported",
    "prompt": "is server-side state and is not supported",
    "metadata": "is not supported",
    "audio": "is not supported",
    "functions": "is deprecated; use tools",
    "function_call": "is deprecated; use tool_choice",
}
_ROLES = frozenset({"system", "developer", "user", "assistant", "tool"})
_TOOL_STRINGS = frozenset({"none", "auto", "required"})
_VOICE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_LANGUAGE = re.compile(r"[a-z]{2,3}")
_MAX_SPEECH_CHARS = 4096
# Image detail levels the OpenAI vision guide documents; an unknown one could bill more (M-14).
_DETAIL = frozenset({"auto", "low", "high", "original"})


def _obj(value: Json, param: str) -> Body:
    if not isinstance(value, Mapping):
        _refuse(param, "must be an object")
    return value


def _list(value: Json, param: str) -> list[Json]:
    if not isinstance(value, list):
        _refuse(param, "must be a list")
    return value


def _only(obj: Body, allowed: Collection[str], param: str = "") -> None:
    for key in obj:
        if key not in allowed:
            name = f"{param}.{key}" if param else key
            _refuse(name, _REFUSED_WHY.get(key, "is not supported"))


def _one_of(value: Json, allowed: Collection[str], param: str) -> None:
    if not isinstance(value, str) or value not in allowed:
        _refuse(param, f"must be one of {', '.join(sorted(allowed))}")


def _data_url(value: Json, param: str, *, remote_ok: bool) -> None:
    # Remote media would make the provider fetch a client-chosen URL (SSRF). Inline media must be
    # an image even with `remote_ok`: only images are priced (M-14), video or PDF bills far more.
    if remote_ok and isinstance(value, str) and is_remote(value):
        return
    if inline_image(value) is None:
        _refuse(param, IMAGE_URL_RULE)


# --- shared pieces -------------------------------------------------------------------------------


def _function(fn: Body, param: str) -> None:
    _only(fn, {"name", "description", "parameters", "strict"}, param)


def _tools(tools: Json, *, flat: bool) -> None:
    """Function tools only. Chat nests `function{}`; Responses puts the fields on the tool."""
    for i, raw in enumerate(_list(tools, "tools")):
        tool, param = _obj(raw, f"tools[{i}]"), f"tools[{i}]"
        if tool.get("type") != "function":
            _refuse(f"{param}.type", "must be function (hosted tools are not supported)")
        if flat:
            _only(tool, {"type", "name", "description", "parameters", "strict"}, param)
        else:
            _only(tool, {"type", "function"}, param)
            _function(_obj(tool.get("function"), f"{param}.function"), f"{param}.function")


def _tool_choice(choice: Json, *, flat: bool) -> None:
    if isinstance(choice, str):
        return _one_of(choice, _TOOL_STRINGS, "tool_choice")
    obj = _obj(choice, "tool_choice")
    _only(obj, {"type", "name"} if flat else {"type", "function"}, "tool_choice")
    if obj.get("type") != "function":
        _refuse("tool_choice.type", "must be function")
    return None


def _exact_list(value: Json, allowed: list[Json], param: str) -> None:
    if value != allowed:
        _refuse(param, f"must be {json.dumps(allowed)}")


def _stream_flag(body: Body) -> None:
    if "stream" in body and not isinstance(body["stream"], bool):
        _refuse("stream", "must be a boolean")


# --- chat ----------------------------------------------------------------------------------------


def _chat_part(raw: Json, param: str, *, remote_ok: bool) -> None:
    part = _obj(raw, param)
    kind = part.get("type")
    if kind == "text":
        _only(part, {"type", "text"}, param)
    elif kind == "image_url":
        _only(part, {"type", "image_url"}, param)
        image = _obj(part.get("image_url"), f"{param}.image_url")
        _only(image, {"url", "detail"}, f"{param}.image_url")
        _data_url(image.get("url"), f"{param}.image_url.url", remote_ok=remote_ok)
        if "detail" in image:
            _one_of(image["detail"], _DETAIL, f"{param}.image_url.detail")
    else:  # input_audio and file parts land here
        _refuse(f"{param}.type", "must be text or image_url")


def _chat_message(raw: Json, i: int, *, remote_ok: bool) -> None:
    param = f"messages[{i}]"
    msg = _obj(raw, param)
    _only(msg, {"role", "content", "name", "tool_calls", "tool_call_id", "refusal"}, param)
    _one_of(msg.get("role"), _ROLES, f"{param}.role")
    content = msg.get("content")
    if isinstance(content, list):
        for j, part in enumerate(content):
            _chat_part(part, f"{param}.content[{j}]", remote_ok=remote_ok)
    elif content is not None and not isinstance(content, str):
        _refuse(f"{param}.content", "must be a string or a list of parts")
    for k, call in enumerate(_list(msg.get("tool_calls", []), f"{param}.tool_calls")):
        c = _obj(call, f"{param}.tool_calls[{k}]")
        _only(c, {"id", "type", "function"}, f"{param}.tool_calls[{k}]")
        _only(_obj(c.get("function"), f"{param}.tool_calls[{k}].function"), {"name", "arguments"})


def _chat(body: Body, *, remote_ok: bool) -> None:
    _only(body, _CHAT_KEYS)
    messages = _list(body.get("messages"), "messages")
    if not messages:
        _refuse("messages", "must not be empty")
    for i, raw in enumerate(messages):
        _chat_message(raw, i, remote_ok=remote_ok)
    if body.get("n", 1) != 1:
        _refuse("n", "must be 1")
    if body.get("store") not in (None, False):
        _refuse("store", "must be false")
    if "modalities" in body:
        _exact_list(body["modalities"], ["text"], "modalities")
    if "response_format" in body:
        fmt = _obj(body["response_format"], "response_format")
        _only(fmt, {"type", "json_schema"}, "response_format")
        _one_of(fmt.get("type"), {"text", "json_object", "json_schema"}, "response_format.type")
    if "tools" in body:
        _tools(body["tools"], flat=False)
    if "tool_choice" in body:
        _tool_choice(body["tool_choice"], flat=False)
    if "stream_options" in body:
        opts = _obj(body["stream_options"], "stream_options")
        _only(opts, {"include_usage"}, "stream_options")
        if body.get("stream") is not True:
            _refuse("stream_options", "needs stream=true")
    _stream_flag(body)


# --- responses -----------------------------------------------------------------------------------

_ITEM_KEYS: Mapping[str | None, frozenset[str]] = {
    "message": frozenset({"type", "role", "content", "id", "status"}),
    None: frozenset({"role", "content"}),
    "function_call": frozenset({"type", "call_id", "name", "arguments", "id", "status"}),
    "function_call_output": frozenset({"type", "call_id", "output", "id", "status"}),
    "reasoning": frozenset({"type", "id", "summary", "encrypted_content", "status"}),
}
_PART_KEYS = {
    "input_text": {"type", "text"},
    "output_text": {"type", "text", "annotations", "logprobs"},
    "input_image": {"type", "image_url", "detail"},
}


def _response_item(raw: Json, i: int, *, remote_ok: bool) -> None:
    param = f"input[{i}]"
    item = _obj(raw, param)
    kind = item.get("type")
    # `isinstance` first: a list/object `type` is unhashable and would raise in the lookup (FZ-01).
    if not isinstance(kind, str | None) or kind not in _ITEM_KEYS:
        _refuse(f"{param}.type", "is not a supported input item")
    _only(item, _ITEM_KEYS[kind if isinstance(kind, str) else None], param)
    # `function_call_output.output` is a second place a content-part list can hide (SR-04).
    for field in ("content", "output"):
        parts = item.get(field)
        if isinstance(parts, list):
            _response_parts(parts, f"{param}.{field}", remote_ok=remote_ok)
        elif field == "output" and parts is not None and not isinstance(parts, str):
            _refuse(f"{param}.output", "must be a string or a list of parts")


def _response_parts(parts: list[Json], param: str, *, remote_ok: bool) -> None:
    """Same allowlist everywhere: text and image parts, images inline unless `remote_ok`."""
    for j, part in enumerate(parts):
        p = _obj(part, f"{param}[{j}]")
        ptype = p.get("type")
        if not isinstance(ptype, str) or ptype not in _PART_KEYS:
            _refuse(f"{param}[{j}].type", "is not a supported content part")  # input_file too
        _only(p, _PART_KEYS[ptype], f"{param}[{j}]")
        if ptype == "input_image":
            _data_url(p.get("image_url"), f"{param}[{j}].image_url", remote_ok=remote_ok)
            if "detail" in p:
                _one_of(p["detail"], _DETAIL, f"{param}[{j}].detail")


def _responses(body: Body, *, remote_ok: bool) -> None:
    _only(body, _RESPONSES_KEYS)
    items = body.get("input")
    if isinstance(items, list):
        for i, raw in enumerate(items):
            _response_item(raw, i, remote_ok=remote_ok)
    elif not isinstance(items, str):
        _refuse("input", "must be a string or a list of items")
    if "instructions" in body and not isinstance(body["instructions"], str):
        _refuse("instructions", "must be a string")  # no content parts outside `input` (M-14)
    if body.get("store") not in (None, False):
        _refuse("store", "must be false")
    if "text" in body:
        _only(_obj(body["text"], "text"), {"format", "verbosity"}, "text")
    if "reasoning" in body:
        _only(_obj(body["reasoning"], "reasoning"), {"effort", "summary", "generate_summary"})
    if "include" in body:
        _exact_list(body["include"], ["reasoning.encrypted_content"], "include")
    if "tools" in body:
        _tools(body["tools"], flat=True)
    if "tool_choice" in body:
        _tool_choice(body["tool_choice"], flat=True)
    _stream_flag(body)


# --- embeddings, transcriptions, speech ----------------------------------------------------------


def _embeddings(body: Body) -> None:
    _only(body, _EMBEDDING_KEYS)
    items = body.get("input")
    if isinstance(items, list):
        ok = bool(items) and all(isinstance(x, str | int | list) for x in items)
    else:
        ok = isinstance(items, str) and bool(items)
    if not ok:
        _refuse("input", "must be a string or a non-empty list of strings or token arrays")
    if "encoding_format" in body:
        _one_of(body["encoding_format"], {"float", "base64"}, "encoding_format")


def _transcription(body: Body, files: Mapping[str, bytes]) -> None:
    _only(body, _TRANSCRIPTION_KEYS)
    if set(files) != {"file"} or not files["file"]:
        _refuse("file", "is required (exactly one file part)")
    if "language" in body and not _LANGUAGE.fullmatch(str(body["language"])):
        _refuse("language", "must be an ISO 639-1 code")
    if "response_format" in body:
        _one_of(
            body["response_format"],
            {"json", "text", "srt", "verbose_json", "vtt"},
            "response_format",
        )
    for g in _as_list(body.get("timestamp_granularities[]", [])):
        _one_of(g, {"word", "segment"}, "timestamp_granularities[]")


def _speech(body: Body) -> None:
    _only(body, _SPEECH_KEYS)
    text = body.get("input")
    if not isinstance(text, str) or not 0 < len(text) <= _MAX_SPEECH_CHARS:
        _refuse("input", f"must be 1 to {_MAX_SPEECH_CHARS} characters")
    if not isinstance(body.get("voice"), str) or not _VOICE.fullmatch(str(body["voice"])):
        _refuse("voice", "must be a voice name")
    if "response_format" in body:
        _one_of(
            body["response_format"], {"mp3", "opus", "aac", "flac", "wav", "pcm"}, "response_format"
        )
    if "stream_format" in body:
        _one_of(body["stream_format"], {"audio"}, "stream_format")


def _as_list(value: Json) -> list[Json]:
    return value if isinstance(value, list) else [value]


def validate(
    endpoint: str, body: Body, files: Mapping[str, bytes] | None = None, *, remote_ok: bool = False
) -> Refusal | None:
    """The allowlist for one endpoint; None when the request may be routed."""
    try:
        check(endpoint, body, files or {}, remote_ok=remote_ok)
    except Untranslatable as refused:
        return refused.refusal
    return None


def check(
    endpoint: str, body: Body, files: Mapping[str, bytes], *, remote_ok: bool = False
) -> None:
    """Like `validate` but raises `Untranslatable`; `translate` repeats it, so no path skips it."""
    if endpoint == CHAT:
        _chat(body, remote_ok=remote_ok)
    elif endpoint == RESPONSES:
        _responses(body, remote_ok=remote_ok)
    elif endpoint == EMBEDDINGS:
        _embeddings(body)
    elif endpoint == TRANSCRIPTIONS:
        _transcription(body, files)
    elif endpoint == SPEECH:
        _speech(body)
    else:
        _refuse("endpoint", "is not an OpenAI endpoint")


# --- rebuilding the upstream body ----------------------------------------------------------------

_ALLOWED: Mapping[str, frozenset[str]] = {
    CHAT: _CHAT_KEYS, RESPONSES: _RESPONSES_KEYS, EMBEDDINGS: _EMBEDDING_KEYS,
    TRANSCRIPTIONS: _TRANSCRIPTION_KEYS, SPEECH: _SPEECH_KEYS,
}  # fmt: skip


# The one ceiling field each endpoint forwards; every other spelling is dropped (M-15).
_CEILING_FIELD: Mapping[str, str] = {CHAT: "max_completion_tokens", RESPONSES: "max_output_tokens"}
_CEILING_SPELLINGS = frozenset({"max_tokens", "max_completion_tokens", "max_output_tokens"})


def rebuild(req: CleanRequest, model: str) -> dict[str, Json]:
    """A fresh body from validated fields only, with the provider's model and the forced fields.

    The client's ceiling fields are dropped and `req.output_ceiling`, the one the hold was sized
    with, is sent alone as `max_completion_tokens` (chat): OpenAI honours it over `max_tokens`,
    so forwarding both would let the larger one run past the hold, and reasoning models reject
    `max_tokens` outright.
    """
    endpoint = req.endpoint
    allowed = _ALLOWED[endpoint]
    out: dict[str, Json] = {
        k: v for k, v in req.body.items() if k in allowed and k not in _CEILING_SPELLINGS
    }
    if (name := _CEILING_FIELD.get(endpoint)) is not None:
        out[name] = held_ceiling(req, name)  # refuses a missing, boolean or non-positive one
    out["model"] = model
    if endpoint in (CHAT, RESPONSES):
        out["store"] = False  # never provider-side storage
    if endpoint == CHAT and out.get("stream") is True:
        out["stream_options"] = {"include_usage": True}
    return out


def encode(req: CleanRequest, model: str) -> tuple[str, bytes]:
    """Content type and bytes of the upstream body, shared by OpenAI and Azure OpenAI."""
    fields = rebuild(req, model)
    if req.endpoint == TRANSCRIPTIONS:
        return multipart(fields, req.files["file"])
    return "application/json", json.dumps(fields, separators=(",", ":")).encode()


def injected_include_usage(endpoint: str, body: Body) -> bool:
    """True when `rebuild` adds `include_usage` the client did not ask for (strip its chunk)."""
    options = body.get("stream_options")
    asked = isinstance(options, Mapping) and options.get("include_usage") is True
    return endpoint == CHAT and body.get("stream") is True and not asked


def multipart(fields: Body, audio: bytes) -> tuple[str, bytes]:
    """Content type and body of a transcription upload. The extension comes from the bytes."""
    try:
        container = measure(audio).container
    except AudioError as exc:
        raise Untranslatable(str(exc), "file") from exc
    boundary = secrets.token_hex(16)
    chunks: list[bytes] = []
    for name, value in fields.items():
        for v in _as_list(value):
            text = str(v).lower() if isinstance(v, bool) else str(v)
            chunks.append(_part(boundary, name, "", text.encode()))
    file_head = f'; filename="audio.{container}"\r\nContent-Type: application/octet-stream'
    chunks.append(_part(boundary, "file", file_head, audio))
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def _part(boundary: str, name: str, extra: str, data: bytes) -> bytes:
    head = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"{extra}\r\n\r\n'
    return head.encode() + data + b"\r\n"


def speech_usage(body: Body) -> Usage | None:
    """Speech answers with audio bytes and no usage; it is billed by the input's characters.

    An input that is not text cannot be counted: None (the full hold), never 0 characters.
    """
    text = body.get("input")
    return Usage(characters=len(text)) if isinstance(text, str) else None
