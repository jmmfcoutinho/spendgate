"""Anthropic adapter: native `/v1/messages`, plain and streamed (protocol.md 2.7).

`validate()` is pure and runs in the pipeline before routing; it checks the body against an
allowlist and the version and beta headers against the constructor's `AnthropicConfig`.
`translate()` runs per routed candidate and checks the headers again.
Anthropic counts `input_tokens` apart from cache reads and writes; core `Usage` wants cached and
cache-write tokens as subsets of `input_tokens`, so the adapter adds them back.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass

from spendgate.core.cost import Usage
from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.media import IMAGE_MIME_TYPES, is_base64
from spendgate.providers.port import (
    AuthMode,
    CleanRequest,
    Deployment,
    Json,
    SseEvent,
    SseLimits,
    StreamRelay,
    Untranslatable,
    UpstreamError,
    UpstreamRequest,
    UpstreamResponse,
    count_of,
    held_ceiling,
    load_json,
    never_raises_error,
    never_raises_response,
    never_raises_usage,
    retry_after_seconds,
)
from spendgate.providers.sse import SseRelay

PATH = "/v1/messages"
VERSION_HEADER = "anthropic-version"
BETA_HEADER = "anthropic-beta"
# Checked 2026-11-17: platform.claude.com/docs/en/api/versioning lists 2026-10-21 as the latest.
CURRENT_VERSION = "2026-10-21"
# From this version on Anthropic answers 400 to temperature, top_p and top_k on every model.
SAMPLING_REMOVED_IN = "2026-10-21"
_THROTTLED = frozenset({429, 529})
_UNAVAILABLE_4XX = frozenset(
    {401, 402, 403, 404}
)  # our key, billing or model config, not the caller

_TOP = frozenset(
    {
        "model",
        "max_tokens",
        "messages",
        "system",
        "stop_sequences",
        "stream",
        "tools",
        "tool_choice",
        "thinking",
        "output_config",
        "metadata",
        "temperature",
        "top_p",
        "top_k",
    }
)
_CACHE_KEYS = ("cache_creation_input_tokens", "cache_read_input_tokens")
_SAMPLING = ("temperature", "top_p", "top_k")
# Options that change what Anthropic bills (or run tools it bills) and that the catalog does not
# price: refused by name so the client learns why, not just "unknown field".
_PRICED_OPTIONS: Mapping[str, str] = {
    "inference_geo": "US-only inference is billed at 1.1x and the catalog has no such price",
    "speed": "fast mode is billed at premium rates and the catalog has no such price",
    "service_tier": "service tiers are not supported",
    "container": "code execution containers are billed separately and not supported",
    "mcp_servers": "MCP connectors are not supported",
    "context_management": "context management is not supported",
    "cache_control": "top-level automatic caching is not supported; set cache_control on blocks",
}
_BLOCK_KEYS: Mapping[str, frozenset[str]] = {
    "text": frozenset({"type", "text", "cache_control", "citations"}),
    "image": frozenset({"type", "source", "cache_control"}),
    "tool_use": frozenset({"type", "id", "name", "input", "cache_control"}),
    "tool_result": frozenset({"type", "tool_use_id", "content", "is_error", "cache_control"}),
    "thinking": frozenset({"type", "thinking", "signature"}),
    "redacted_thinking": frozenset({"type", "data"}),
}
_TOOL_KEYS = frozenset({"type", "name", "description", "input_schema", "cache_control", "strict"})


@dataclass(frozen=True, slots=True)
class AnthropicConfig:
    """`anthropic.*` config: the only versions and betas a client may send."""

    # Older SDKs send 2023-06-01; a version changes no price, so refusing it only adds friction.
    allowed_versions: tuple[str, ...] = ("2023-06-01", CURRENT_VERSION)
    allowed_betas: frozenset[str] = frozenset()
    default_version: str = CURRENT_VERSION  # used when the client sends none

    def __post_init__(self) -> None:
        if self.default_version not in self.allowed_versions:
            raise ValueError("anthropic.default_version must be in allowed_versions")


def _obj(value: Json, path: str) -> Mapping[str, Json]:
    if not isinstance(value, Mapping):
        raise Untranslatable(f"{path} must be an object", path)
    return value


def _only(obj: Mapping[str, Json], allowed: frozenset[str], path: str) -> None:
    for key in obj:
        if key not in allowed:
            name = f"{path}.{key}" if path else key
            raise Untranslatable(f"{name} is not supported", name)


def _list(value: Json, path: str) -> list[Json]:
    if not isinstance(value, list) or not value:
        raise Untranslatable(f"{path} must be a non-empty list", path)
    return value


def _number(value: Json, path: str, *, integer: bool = False) -> None:
    ok = isinstance(value, int) if integer else isinstance(value, int | float)
    if isinstance(value, bool) or not ok:
        raise Untranslatable(f"{path} must be a number", path)


def _cache_control(value: Json, path: str) -> None:
    cc = _obj(value, path)
    _only(cc, frozenset({"type", "ttl"}), path)
    if cc.get("type") != "ephemeral" or cc.get("ttl", "5m") not in ("5m", "1h"):
        raise Untranslatable(f"{path} must be ephemeral with ttl 5m or 1h", path)


def _block(value: Json, path: str, *, kinds: frozenset[str]) -> None:
    block = _obj(value, path)
    kind = block.get("type")
    if not isinstance(kind, str) or kind not in kinds:
        raise Untranslatable(f"{path}.type is not supported", f"{path}.type")
    _only(block, _BLOCK_KEYS[kind], path)
    if "cache_control" in block:
        _cache_control(block["cache_control"], f"{path}.cache_control")
    if kind == "image":  # url and file sources would make Anthropic fetch from the network
        source = _obj(block.get("source"), f"{path}.source")
        _only(source, frozenset({"type", "media_type", "data"}), f"{path}.source")
        if source.get("type") != "base64":
            raise Untranslatable("only base64 image sources are supported", f"{path}.source.type")
        media = source.get("media_type")  # only images are priced (M-14); fail closed on others
        if not isinstance(media, str) or media not in IMAGE_MIME_TYPES:
            where = f"{path}.source.media_type"
            raise Untranslatable(f"{where} must be image/png, jpeg, webp or gif", where)
        if not is_base64(source.get("data")):  # the same payload rule as the data URLs
            where = f"{path}.source.data"
            raise Untranslatable(f"{where} must be base64 on one line", where)
    if kind == "tool_result" and isinstance(block.get("content"), list):
        _blocks(block["content"], f"{path}.content", frozenset({"text", "image"}))


def _blocks(value: Json, path: str, kinds: frozenset[str]) -> None:
    for i, item in enumerate(_list(value, path)):
        _block(item, f"{path}[{i}]", kinds=kinds)


def _messages(value: Json) -> None:
    every = frozenset(_BLOCK_KEYS)
    for i, item in enumerate(_list(value, "messages")):
        msg = _obj(item, f"messages[{i}]")
        _only(msg, frozenset({"role", "content"}), f"messages[{i}]")
        if msg.get("role") not in ("user", "assistant"):
            raise Untranslatable("role must be user or assistant", f"messages[{i}].role")
        if not isinstance(msg.get("content"), str):
            _blocks(msg.get("content"), f"messages[{i}].content", every)


def _tools(value: Json) -> None:
    for i, item in enumerate(_list(value, "tools")):
        path = f"tools[{i}]"
        tool = _obj(item, path)
        # Server tools (web_search, code_execution, ...) carry a versioned type and bill per use.
        _only(tool, _TOOL_KEYS, path)
        if tool.get("type", "custom") != "custom":
            raise Untranslatable("only custom tools are supported", f"{path}.type")
        if "cache_control" in tool:
            _cache_control(tool["cache_control"], f"{path}.cache_control")


def _small_objects(body: Mapping[str, Json]) -> None:
    specs = {
        "thinking": frozenset({"type", "budget_tokens", "display"}),
        "tool_choice": frozenset({"type", "name", "disable_parallel_tool_use"}),
        "output_config": frozenset({"effort", "format"}),
        "metadata": frozenset({"user_id"}),
    }
    for key, allowed in specs.items():
        if key in body:
            _only(_obj(body[key], key), allowed, key)


def _body(body: Mapping[str, Json], version: str) -> None:
    for key, why in _PRICED_OPTIONS.items():
        if key in body:
            raise Untranslatable(f"{key}: {why}", key)
    _only(body, _TOP, "")
    if not isinstance(body.get("model"), str) or not body["model"]:
        raise Untranslatable("model is required", "model")
    _number(body.get("max_tokens"), "max_tokens", integer=True)
    _messages(body.get("messages"))
    for key in _SAMPLING:
        if key in body:
            if version >= SAMPLING_REMOVED_IN:
                raise Untranslatable(f"{key} is rejected by anthropic-version {version}", key)
            _number(body[key], key)
    if "stream" in body and not isinstance(body["stream"], bool):
        raise Untranslatable("stream must be a boolean", "stream")
    if "system" in body and not isinstance(body["system"], str):
        _blocks(body["system"], "system", frozenset({"text"}))
    if "tools" in body:
        _tools(body["tools"])
    _small_objects(body)


def _version(headers: Mapping[str, str], config: AnthropicConfig) -> str:
    """The client's version header (any case) when allowed, else the configured default."""
    version = {k.lower(): v for k, v in headers.items()}.get(VERSION_HEADER)
    if version is None:
        return config.default_version
    if version not in config.allowed_versions:
        raise Untranslatable(f"anthropic-version {version!r} is not allowed", VERSION_HEADER)
    return version


def _betas(headers: Mapping[str, str], config: AnthropicConfig) -> str | None:
    value = {k.lower(): v for k, v in headers.items()}.get(BETA_HEADER, "")
    raw = [b.strip() for b in value.split(",") if b.strip()]
    for beta in raw:
        if beta not in config.allowed_betas:
            raise Untranslatable(f"anthropic-beta {beta!r} is not allowed", BETA_HEADER)
    return ",".join(raw) or None


def _count(obj: Mapping[str, Json], key: str) -> int | None:
    return count_of(obj.get(key))


def _normalise(input_tokens: int, output: int, cache_write: int, cache_read: int) -> Usage:
    return Usage(
        input_tokens=input_tokens + cache_write + cache_read,
        output_tokens=output,
        cached_input_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


def _usage_obj(value: Json) -> Mapping[str, Json] | None:
    return value if isinstance(value, Mapping) else None


class _Collector:
    """`message_start` carries input and cache usage; `message_delta` the cumulative output."""

    def __init__(self) -> None:
        self._fields: dict[str, int] = {}
        self._output_final = False
        self._broken = False

    def feed(self, event: SseEvent) -> None:
        if event.event not in ("message_start", "message_delta"):
            return
        data = load_json(event.data)
        if not isinstance(data, Mapping):
            self._broken = True
            return
        source = data.get("message") if event.event == "message_start" else data
        usage = _usage_obj(source.get("usage")) if isinstance(source, Mapping) else None
        if usage is None:
            return
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            if key in usage:
                self._take(usage, key)
        if event.event == "message_delta":
            self._output_final = self._take(usage, "output_tokens") or self._output_final

    def _take(self, usage: Mapping[str, Json], key: str) -> bool:
        count = _count(usage, key)
        if count is None:
            self._broken = True  # garbled numbers: fall back to the full hold
            return False
        self._fields[key] = count  # cumulative: the last value wins
        return True

    def result(self) -> Usage | None:
        f = self._fields
        if self._broken or not self._output_final or "input_tokens" not in f:
            return None
        return _normalise(
            f["input_tokens"],
            f["output_tokens"],
            f.get("cache_creation_input_tokens", 0),
            f.get("cache_read_input_tokens", 0),
        )


def _error_message(body: bytes) -> str:
    data = load_json(body)
    error = data.get("error") if isinstance(data, Mapping) else None
    message = error.get("message") if isinstance(error, Mapping) else None
    return message[:500] if isinstance(message, str) else ""


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, config: AnthropicConfig | None = None) -> None:
        self._config = config or AnthropicConfig()

    def validate(self, req: CleanRequest) -> Refusal | None:
        """Check a `/v1/messages` request against the allowlist; header names match any case."""
        try:
            if req.endpoint != PATH:
                raise Untranslatable("endpoint is not an Anthropic endpoint", "endpoint")
            version = _version(req.headers, self._config)
            _betas(req.headers, self._config)
            _body(req.body, version)
        except Untranslatable as refused:
            return refused.refusal
        return None

    async def prepare(self, deployment: Deployment) -> None:
        """A static API key: nothing to refresh."""

    def translate(self, req: CleanRequest, deployment: Deployment) -> UpstreamRequest:
        if deployment.provider != self.name or deployment.auth is not AuthMode.API_KEY:
            raise ValueError("deployment is not an Anthropic API-key deployment")
        if not deployment.credential:
            raise ValueError("deployment has no credential")  # fail closed before any contact
        version = _version(req.headers, self._config)
        headers = {
            "content-type": "application/json",
            "x-api-key": deployment.credential,
            VERSION_HEADER: version,
        }
        if beta := _betas(req.headers, self._config):
            headers[BETA_HEADER] = beta
        # Rebuilt from the allowlist, never copied: any other ceiling spelling is dropped and
        # `max_tokens` is the ceiling the hold was sized with, not a client value (M-15).
        body: dict[str, Json] = {k: v for k, v in req.body.items() if k in _TOP}
        body["model"] = deployment.model
        body["max_tokens"] = held_ceiling(req, "max_tokens")
        return UpstreamRequest(
            method="POST",
            url=deployment.base_url.rstrip("/") + PATH,  # base from config, path fixed
            headers=headers,
            body=json.dumps(body).encode(),
            stream=req.stream,
        )

    @never_raises_response
    def response(self, req: CleanRequest, resp: UpstreamResponse) -> UpstreamResponse:
        return resp  # already the client's shape

    def relay(self, req: CleanRequest, limits: SseLimits = SseLimits()) -> StreamRelay:  # noqa: B008
        return SseRelay(_Collector(), limits)

    @never_raises_usage
    def usage(self, req: CleanRequest, response: UpstreamResponse) -> Usage | None:
        data = load_json(response.body)
        usage = _usage_obj(data.get("usage")) if isinstance(data, Mapping) else None
        if usage is None:
            return None
        # The 5m and 1h cache-write split is ignored: the catalog has one cache-write price,
        # the dearer 1h one, so every cache-write token is billed at it (never under-charged).
        # A cache field that is present but garbled makes the usage unreadable (full hold).
        cache = [_count(usage, k) if k in usage else 0 for k in _CACHE_KEYS]
        base = (_count(usage, "input_tokens"), _count(usage, "output_tokens"))
        if None in cache or None in base:
            return None
        return _normalise(base[0] or 0, base[1] or 0, cache[0] or 0, cache[1] or 0)

    @never_raises_error
    def error(self, response: UpstreamResponse) -> UpstreamError:
        status = response.status
        if status in _THROTTLED:
            code = ErrorCode.UPSTREAM_THROTTLED
        elif status >= 500 or status in _UNAVAILABLE_4XX:  # noqa: PLR2004
            code = ErrorCode.UPSTREAM_UNAVAILABLE
        else:
            code = ErrorCode.INVALID_REQUEST
        return UpstreamError(
            code,
            status=status,
            retry_after_seconds=retry_after_seconds(response.headers),
            message=_error_message(response.body),
        )
