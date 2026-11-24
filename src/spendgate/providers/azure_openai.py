"""Azure OpenAI adapter: the OpenAI wire shapes over Azure URLs, auth and deployment names.

Bodies, allowlists, forced fields, usage and error mapping come from the shared `openai_*`
modules, so Azure cannot drift from OpenAI. What differs is here:

* URL. The config `base_url` is the resource (`https://<name>.openai.azure.com`) and the path is
  fixed. `api_version` set means the classic API
  (`/openai/deployments/<deployment>/...?api-version=`, Responses at `/openai/responses`);
  absent means the v1 API (`/openai/v1/...`, no query). The deployment name is
  `Deployment.model`; the client's alias never reaches the URL (SSRF, naming).
* Auth. `api-key` header for API keys; `Authorization: Bearer` for Entra ID, from an injected or
  default credential (see `azure_openai_auth`), or a static token when the config gives one.
* Errors. Azure sends `retry-after-ms`; a 400 `content_filter` is a client error (not retried).
"""

from collections.abc import Mapping
from urllib.parse import quote

from spendgate.core.cost import Usage
from spendgate.core.errors import ErrorCode, Refusal
from spendgate.providers import openai_shapes as shapes
from spendgate.providers import openai_usage as wire
from spendgate.providers.azure_openai_auth import (
    CLASSIC_SCOPE,
    V1_SCOPE,
    EntraTokens,
    TokenCredential,
)
from spendgate.providers.port import (
    AuthMode,
    CleanRequest,
    Deployment,
    SseLimits,
    StreamRelay,
    UpstreamError,
    UpstreamRequest,
    UpstreamResponse,
    never_raises_error,
    never_raises_response,
    never_raises_usage,
    retry_after_ms_seconds,
)
from spendgate.providers.refresh import Refreshes
from spendgate.providers.sse import SseRelay

# Classic path tail per endpoint; the deployment goes in the URL for all but Responses.
_CLASSIC_TAIL: Mapping[str, str] = {
    shapes.CHAT: "chat/completions",
    shapes.EMBEDDINGS: "embeddings",
    shapes.TRANSCRIPTIONS: "audio/transcriptions",
    shapes.SPEECH: "audio/speech",
}
_HTTP_OK_MAX = 299


def resource_root(base_url: str) -> str:
    """The resource URL, tolerating a base that already ends in /openai or /openai/v1."""
    root = base_url.rstrip("/")
    for suffix in ("/v1", "/openai"):
        root = root.removesuffix(suffix)
    return root


def endpoint_url(deployment: Deployment, endpoint: str) -> str:
    root = resource_root(deployment.base_url)
    version = deployment.api_version
    if not version:
        return f"{root}/openai{endpoint}"  # /openai/v1/<endpoint tail>; the model is in the body
    query = f"?api-version={quote(version, safe='')}"
    if endpoint == shapes.RESPONSES:
        return f"{root}/openai/responses{query}"
    name = quote(deployment.model, safe="")
    return f"{root}/openai/deployments/{name}/{_CLASSIC_TAIL[endpoint]}{query}"


def _scope(deployment: Deployment) -> str:
    return CLASSIC_SCOPE if deployment.api_version else V1_SCOPE


class AzureOpenAIProvider:
    name = "azure_openai"

    def __init__(
        self, *, remote_ok: bool = False, credential: TokenCredential | None = None
    ) -> None:
        self._remote_ok = remote_ok  # config: may image parts be remote URLs (provider fetches)
        self._tokens = EntraTokens(credential)
        self._refreshes = Refreshes()

    def __repr__(self) -> str:
        return "AzureOpenAIProvider()"

    def validate(self, req: CleanRequest) -> Refusal | None:
        return shapes.validate(req.endpoint, req.body, req.files, remote_ok=self._remote_ok)

    async def prepare(self, deployment: Deployment) -> None:
        """Fetch the Entra token in a worker thread when the cache has none fresh."""
        if deployment.auth is not AuthMode.BEARER_TOKEN or deployment.credential:
            return  # an API key or a configured static token: nothing to refresh
        scope = _scope(deployment)
        stale = lambda: not self._tokens.fresh(scope)  # noqa: E731 - a one-line check
        await self._refreshes.ensure(scope, stale, lambda: self._tokens.bearer(scope))

    def translate(self, req: CleanRequest, deployment: Deployment) -> UpstreamRequest:
        """Raises `Untranslatable` for a request that cannot be carried."""
        shapes.check(req.endpoint, req.body, req.files, remote_ok=self._remote_ok)
        auth = self._auth_headers(deployment)  # after the checks: a refusal needs no token
        # The body model is the deployment name: the v1 path routes on it, classic ignores it.
        content_type, body = shapes.encode(req, deployment.model)
        headers = {
            **auth,
            "Content-Type": content_type,
            "Accept": "text/event-stream" if req.stream else "application/json",
        }
        url = endpoint_url(deployment, req.endpoint)
        return UpstreamRequest("POST", url, headers, body, stream=req.stream)

    def _auth_headers(self, deployment: Deployment) -> dict[str, str]:
        if deployment.auth is AuthMode.API_KEY:
            if not deployment.credential:
                raise ValueError("the deployment has no API key")  # misconfiguration, fail closed
            return {"api-key": deployment.credential}
        if deployment.auth is AuthMode.BEARER_TOKEN:
            # A configured credential is a ready-made token; none means Entra ID via azure-identity.
            scope = _scope(deployment)
            # What `prepare` readied, while unexpired; a direct caller without it fetches inline.
            token = (
                deployment.credential or self._tokens.cached(scope) or self._tokens.bearer(scope)
            )
            return {"Authorization": f"Bearer {token}"}
        raise ValueError(f"azure_openai does not support auth mode {deployment.auth.value}")

    @never_raises_response
    def response(self, req: CleanRequest, resp: UpstreamResponse) -> UpstreamResponse:
        return resp  # already the client's shape

    def relay(self, req: CleanRequest, limits: SseLimits = SseLimits()) -> StreamRelay:  # noqa: B008
        """Relays events unchanged, minus the usage-only chunk when `translate` injected it."""
        injected = shapes.injected_include_usage(req.endpoint, req.body)
        hide = wire.is_usage_only_chunk if injected else None
        return SseRelay(wire.OpenAIStreamUsage(req.endpoint), limits, hide)

    @never_raises_usage
    def usage(self, req: CleanRequest, response: UpstreamResponse) -> Usage | None:
        if req.endpoint == shapes.SPEECH:  # audio bytes carry no usage: billed by input characters
            ok = response.status <= _HTTP_OK_MAX
            return shapes.speech_usage(req.body) if ok else None
        return wire.usage_of(response, req.endpoint)

    @never_raises_error
    def error(self, response: UpstreamResponse) -> UpstreamError:
        """OpenAI mapping, plus Azure's millisecond `retry-after-ms` on throttling.

        A 400 with code `content_filter` already maps to `invalid_request` (a client error that
        costs nothing and is never retried); it is not an upstream fault.
        """
        mapped = wire.map_error(response)
        if mapped.code is not ErrorCode.UPSTREAM_THROTTLED:
            return mapped
        millis = retry_after_ms_seconds(response.headers)
        if millis is None:
            return mapped
        return UpstreamError(mapped.code, True, mapped.status, millis, mapped.message)
