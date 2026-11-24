"""Google adapter: Gemini API (API key) and Vertex AI (service account), chat only.

One adapter, two auth strategies, one wire shape. `translate()` rebuilds a Gemini body from
validated OpenAI chat fields (refusing what it cannot carry, never dropping silently);
`response()` and `relay()` map answers back (`google_shapes`, `google_stream`).
"""

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from http import HTTPStatus
from typing import Protocol

from spendgate.core.cost import Usage
from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.media import IMAGE_MIME_TYPES, inline_image
from spendgate.providers.google_shapes import (
    as_list,
    as_text,
    completion,
    dig,
    has_output,
    load,
    read_usage,
)
from spendgate.providers.google_stream import ChatStream, GoogleRelay
from spendgate.providers.port import (
    AuthMode,
    CleanRequest,
    Deployment,
    Json,
    SseLimits,
    StreamRelay,
    Untranslatable,
    UpstreamError,
    UpstreamRequest,
    UpstreamResponse,
    held_ceiling,
    never_raises_error,
    never_raises_response,
    never_raises_usage,
    retry_after_seconds,
)
from spendgate.providers.refresh import Refreshes
from spendgate.providers.sse import asked_for_usage

CHAT = "/v1/chat/completions"
_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_SAFE = re.compile(r"[A-Za-z0-9._-]+")  # config values that land in the URL path
# Gemini's image-understanding page lists png, jpeg, webp, heic and heif, not gif: a type it does
# not document is refused rather than sent (heic and heif are not priceable images here at all).
_GEMINI_IMAGES = IMAGE_MIME_TYPES - {"image/gif"}
_PASSED = frozenset(
    {"model", "messages", "max_tokens", "max_completion_tokens", "temperature", "top_p", "stop"}
    | {"tools", "tool_choice", "response_format", "stream", "user", "store", "stream_options"}
)
# Statuses that mean our own credentials, project or model name are wrong, not the caller's request.
_OPERATOR_FAULTS = frozenset(
    {"UNAUTHENTICATED", "PERMISSION_DENIED", "NOT_FOUND", "FAILED_PRECONDITION"}
)
_TOOL_KEYS = (("description", "description"), ("parameters", "parametersJsonSchema"))


class Credentials(Protocol):
    token: str | None
    project_id: str | None

    @property
    def valid(self) -> bool: ...

    def refresh(self, request: object) -> None: ...


def _part(part: Json, where: str) -> Json:
    kind = dig(part, "type")
    if kind == "text" and isinstance(dig(part, "text"), str):
        return {"text": dig(part, "text")}
    # Gemini would fetch a remote URL itself (SSRF), so only inline data is allowed; and only an
    # image, since Gemini bills inline video, audio or PDF by its length, not as one image (M-14).
    url = dig(part, "image_url", "url") if kind == "image_url" else None
    image = inline_image(url, _GEMINI_IMAGES)
    if image is None:
        raise Untranslatable(
            "Only text and image parts are supported; images must be an inline base64 "
            "data:image/png, jpeg or webp URL for Gemini.",
            where,
        )
    return {"inlineData": {"mimeType": image[0], "data": image[1]}}


def _parts(content: Json, where: str) -> list[Json]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if not isinstance(content, list):
        raise Untranslatable("Message content must be text or a list of parts.", where)
    return [_part(p, f"{where}[{j}]") for j, p in enumerate(content)]


def _call_part(call: Json, where: str) -> tuple[str, str, Json]:
    """(call id, function name, Gemini functionCall part) for one OpenAI tool call."""
    name = as_text(dig(call, "function", "name"))
    args = load(as_text(dig(call, "function", "arguments")) or "{}")
    if not name or args is None:
        raise Untranslatable("A tool call needs a name and JSON-object arguments.", where)
    part: dict[str, Json] = {"functionCall": {"name": name, "args": args}}
    if isinstance(signature := dig(call, "extra_content", "google", "thought_signature"), str):
        # Gemini 3 needs its thought signature echoed back with the function call.
        part["thoughtSignature"] = signature
    return as_text(dig(call, "id")), name, part


def _tool_result(message: Json, names: Mapping[str, str], where: str) -> Json:
    name = names.get(as_text(dig(message, "tool_call_id")))
    if name is None:
        raise Untranslatable("A tool message must answer an earlier tool call.", where)
    parts = _parts(dig(message, "content"), where)
    text = "".join(as_text(dig(p, "text")) for p in parts)
    response = load(text) or {"result": text}
    return {"functionResponse": {"name": name, "response": response}}


def _contents(messages: Json) -> tuple[list[Json], list[Json]]:
    """Gemini contents plus system parts; consecutive tool results share one user turn."""
    if not isinstance(messages, list) or not messages:
        raise Untranslatable("messages must be a non-empty list.", "messages")
    contents: list[Json] = []
    system: list[Json] = []
    names: dict[str, str] = {}
    results: list[Json] | None = None  # the open user turn collecting tool results
    for i, message in enumerate(messages):
        where, role = f"messages[{i}]", dig(message, "role")
        if role == "tool":
            if results is None:
                results = []
                contents.append({"role": "user", "parts": results})
            results.append(_tool_result(message, names, where))
            continue
        results = None
        parts = _parts(dig(message, "content"), f"{where}.content")
        if role in ("system", "developer"):
            system += parts
        elif role in ("user", "assistant"):
            for k, call in enumerate(as_list(dig(message, "tool_calls"))):
                call_id, name, part = _call_part(call, f"{where}.tool_calls[{k}]")
                names[call_id] = name
                parts.append(part)
            contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})
        else:
            raise Untranslatable("Unsupported message role.", f"{where}.role")
    return contents, system


def _tools(tools: Json) -> list[Json]:
    declarations: list[Json] = []
    for i, tool in enumerate(as_list(tools)):
        where, fn = f"tools[{i}]", dig(tool, "function")
        if dig(tool, "type") != "function" or not as_text(dig(fn, "name")):
            raise Untranslatable("Only function tools are supported.", where)
        if dig(fn, "strict") is True:  # Gemini cannot guarantee strict schemas
            raise Untranslatable(
                "strict function schemas are not supported.", f"{where}.function.strict"
            )
        decl: dict[str, Json] = {"name": dig(fn, "name")}
        decl |= {t: v for s, t in _TOOL_KEYS if (v := dig(fn, s)) is not None}
        declarations.append(decl)
    return [{"functionDeclarations": declarations}]


def _tool_config(choice: Json) -> Json:
    modes = {"auto": "AUTO", "none": "NONE", "required": "ANY"}
    if isinstance(choice, str) and choice in modes:
        return {"functionCallingConfig": {"mode": modes[choice]}}
    name = as_text(dig(choice, "function", "name"))
    if dig(choice, "type") != "function" or not name:
        raise Untranslatable("Unsupported tool_choice.", "tool_choice")
    return {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [name]}}


def _generation_config(body: Mapping[str, Json], ceiling: int) -> dict[str, Json]:
    # The ceiling the hold was sized with; the client's own fields are not read again (M-15).
    config: dict[str, Json] = {"maxOutputTokens": ceiling}
    config |= {
        t: body[s] for s, t in (("temperature", "temperature"), ("top_p", "topP")) if s in body
    }
    if (stop := body.get("stop")) is not None:
        config["stopSequences"] = [stop] if isinstance(stop, str) else stop
    if "response_format" in body:
        kind = dig(body["response_format"], "type")
        if kind == "json_schema":
            config["responseMimeType"] = "application/json"
            config["responseJsonSchema"] = dig(body["response_format"], "json_schema", "schema")
        elif kind != "text":
            raise Untranslatable("Only text and json_schema response formats.", "response_format")
    return config


def _check_fields(body: Mapping[str, Json]) -> None:
    for key in body:
        if key not in _PASSED:
            raise Untranslatable(f"{key} is not supported for Gemini models.", key)


def chat_to_gemini(body: Mapping[str, Json], ceiling: int) -> dict[str, Json]:
    """The Gemini request body for an OpenAI chat body; raises `Untranslatable`.

    `ceiling` is the output ceiling the hold was sized with, sent as the only `maxOutputTokens`.
    """
    _check_fields(body)
    contents, system = _contents(body.get("messages"))
    config = _generation_config(body, ceiling)
    out: dict[str, Json] = {"contents": contents, "generationConfig": config}
    if system:
        out["systemInstruction"] = {"parts": system}
    if body.get("tools"):
        out["tools"] = _tools(body["tools"])
    if "tool_choice" in body:
        out["toolConfig"] = _tool_config(body["tool_choice"])
    return out


def _service_account(deployment: Deployment) -> Credentials:
    """Service-account JSON from the deployment credential (the gateway holds it, nobody else)."""
    from google.oauth2 import service_account  # noqa: PLC0415 - optional extra, Vertex only

    if not deployment.credential:
        raise ValueError("a Vertex deployment needs service-account credentials")
    info = json.loads(deployment.credential)
    creds: Credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
        info, scopes=[_SCOPE]
    )
    return creds


def _digest(deployment: Deployment) -> str:
    return hashlib.sha256(deployment.credential.encode()).hexdigest()


class GoogleProvider:
    """Gemini API key or Vertex AI bearer auth. Credentials are injectable for tests."""

    name = "google"

    def __init__(self, credentials: Callable[[Deployment], Credentials] | None = None) -> None:
        self._source = credentials or _service_account
        self._cache: dict[str, Credentials] = {}  # keyed by a digest, never by the secret
        self._refreshes = Refreshes()

    def validate(self, req: CleanRequest) -> Refusal | None:
        """Chat completions only; field refusals come from `translate`, after the ceiling."""
        if req.endpoint != CHAT:
            return Refusal(
                ErrorCode.INVALID_REQUEST, "Google supports chat completions only.", "endpoint"
            )
        return None

    async def prepare(self, deployment: Deployment) -> None:
        """Vertex: load and refresh the service-account token in a worker thread when stale."""
        if deployment.auth is not AuthMode.BEARER_TOKEN:
            return  # a Gemini API key never expires
        key = _digest(deployment)

        def stale() -> bool:
            creds = self._cache.get(key)
            return creds is None or not creds.valid

        await self._refreshes.ensure(key, stale, lambda: self._credentials(deployment))

    def translate(self, req: CleanRequest, deployment: Deployment) -> UpstreamRequest:
        if (refusal := self.validate(req)) is not None:
            raise Untranslatable(refusal.message, refusal.param)
        gemini = chat_to_gemini(req.body, held_ceiling(req, "max_completion_tokens"))
        body = json.dumps(gemini, separators=(",", ":")).encode()
        method = "streamGenerateContent" if req.stream else "generateContent"
        url, headers = self._target(deployment, method)
        if req.stream:
            url += "?alt=sse"  # SSE framing instead of a JSON array; the key stays in a header
        return UpstreamRequest("POST", url, headers, body, req.stream)

    def _target(self, d: Deployment, method: str) -> tuple[str, dict[str, str]]:
        if not _SAFE.fullmatch(d.model):
            raise ValueError("google model name must be a plain identifier")
        base, headers = d.base_url.rstrip("/"), {"content-type": "application/json"}
        if d.auth is AuthMode.API_KEY:
            if not d.credential:
                raise ValueError("google deployment has no API key")
            url = f"{base}/v1beta/models/{d.model}:{method}"
            return url, headers | {"x-goog-api-key": d.credential}
        if d.auth is not AuthMode.BEARER_TOKEN:
            raise ValueError(f"google does not support auth mode {d.auth.value}")
        creds = self._credentials(d)
        if not d.region or not _SAFE.fullmatch(d.region) or not creds.project_id:
            raise ValueError("a Vertex deployment needs a region and a credential project")
        where = f"projects/{creds.project_id}/locations/{d.region}/publishers/google/models"
        return f"{base}/v1/{where}/{d.model}:{method}", headers | {
            "authorization": f"Bearer {creds.token}"
        }

    def _credentials(self, d: Deployment) -> Credentials:
        key = _digest(d)
        creds = self._cache.get(key) or self._cache.setdefault(key, self._source(d))
        if not creds.valid:  # google-auth treats a token near expiry as invalid (network refresh)
            from google.auth.transport import requests  # noqa: PLC0415 - optional extra

            creds.refresh(requests.Request())
        if not creds.token:
            raise ValueError("Vertex credentials produced no token")
        return creds

    @never_raises_response
    def response(self, req: CleanRequest, resp: UpstreamResponse) -> UpstreamResponse:
        """A complete `generateContent` answer as an OpenAI `chat.completion`."""
        chunk = load(resp.body)
        if chunk is None:
            return UpstreamResponse(
                502, {}, b""
            )  # a 200 we cannot read: the pipeline charges the hold
        body = completion(chunk, req.model, req.response_id, req.created)
        return UpstreamResponse(
            resp.status, {"content-type": "application/json"}, json.dumps(body).encode()
        )

    def relay(self, req: CleanRequest, limits: SseLimits = SseLimits()) -> StreamRelay:  # noqa: B008
        stream = ChatStream(
            req.model, req.response_id, req.created, include_usage=asked_for_usage(req.body)
        )
        return GoogleRelay(stream, limits)

    @never_raises_usage
    def usage(self, req: CleanRequest, response: UpstreamResponse) -> Usage | None:
        body = load(response.body)
        if body is None:
            return None
        return read_usage(body.get("usageMetadata"), output_seen=has_output(body))

    @never_raises_error
    def error(self, response: UpstreamResponse) -> UpstreamError:
        err = dig(load(response.body), "error")
        status_name = as_text(dig(err, "status"))
        # A bad API key arrives as INVALID_ARGUMENT; it is our fault, not the caller's.
        reasons = (as_text(dig(d, "reason")) for d in as_list(dig(err, "details")))
        if (
            response.status == HTTPStatus.TOO_MANY_REQUESTS
        ):  # RESOURCE_EXHAUSTED: nothing was processed
            code = ErrorCode.UPSTREAM_THROTTLED
        elif (
            response.status >= HTTPStatus.INTERNAL_SERVER_ERROR
            or status_name in _OPERATOR_FAULTS
            or any(r.startswith("API_KEY") for r in reasons)
        ):
            code = ErrorCode.UPSTREAM_UNAVAILABLE  # an outage, or our own credentials or billing
        else:
            code = ErrorCode.INVALID_REQUEST  # INVALID_ARGUMENT: the request itself was rejected
        text = as_text(dig(err, "message"))[:500]
        retry = retry_after_seconds(response.headers)
        return UpstreamError(code, True, response.status, retry, text)
