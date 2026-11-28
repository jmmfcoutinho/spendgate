"""Azure OpenAI adapter: the OpenAI wire shapes over Azure URLs, auth and deployment names.

Bodies, allowlists, forced fields, the relay, usage and error mapping come from `OpenAIWire`
(`openai_wire`, over the shared `openai_*` modules), so Azure cannot drift from OpenAI. What
differs is here:

* URL. The config `base_url` is the resource (`https://<name>.openai.azure.com`) and the path is
  fixed. `api_version` set means the classic API
  (`/openai/deployments/<deployment>/...?api-version=`, Responses at `/openai/responses`);
  absent means the v1 API (`/openai/v1/...`, no query). The deployment name is
  `Deployment.model`; the client's alias never reaches the URL (SSRF, naming).
* Auth. `api-key` header for API keys; `Authorization: Bearer` for Entra ID (config
  `credential: entra`, `Deployment.ambient`), from an injected or default credential (see
  `azure_openai_auth`), or a static token when the config gives a secret reference instead. A
  401 or 403 drops the cached Entra tokens, so the next call's `prepare` fetches a new one.
* Errors. Azure sends `retry-after-ms`; a 400 `content_filter` is a client error (not retried);
  Azure's own codes for a missing deployment or a denied identity are deployment faults.
"""

import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from http import HTTPStatus
from urllib.parse import quote

from spendgate.core.errors import ErrorCode
from spendgate.providers import openai_shapes as shapes
from spendgate.providers import openai_usage as wire
from spendgate.providers.azure_openai_auth import (
    CLASSIC_SCOPE,
    V1_SCOPE,
    EntraTokens,
    TokenCredential,
)
from spendgate.providers.openai_wire import OpenAIWire
from spendgate.providers.port import (
    AuthMode,
    CleanRequest,
    Deployment,
    UpstreamError,
    UpstreamRequest,
    UpstreamResponse,
    never_raises_error,
    retry_after_ms_seconds,
)
from spendgate.providers.refresh import Refreshes

# Classic path tail per endpoint; the deployment goes in the URL for all but Responses.
_CLASSIC_TAIL: Mapping[str, str] = {
    shapes.CHAT: "chat/completions",
    shapes.EMBEDDINGS: "embeddings",
    shapes.TRANSCRIPTIONS: "audio/transcriptions",
    shapes.SPEECH: "audio/speech",
}
_REVOKED = (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN)  # perhaps a revoked or stale token
# Azure's own `error.code` for a deployment that does not exist, an Entra identity without a role
# on the resource, and key auth turned off on it: our configuration, whatever was sent (ADR-0033).
_DEPLOYMENT_FAULTS = wire.DEPLOYMENT_FAULTS | {
    "DeploymentNotFound",
    "PermissionDenied",
    "AuthenticationTypeDisabled",
}


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


def _entra(deployment: Deployment) -> bool:
    """Bearer auth from the host's Entra ID identity: `credential: entra` in config, or (library
    callers building a `Deployment` by hand) no stored token at all."""
    return deployment.auth is AuthMode.BEARER_TOKEN and (
        deployment.ambient or not deployment.credential
    )


class AzureOpenAIProvider(OpenAIWire):
    name = "azure_openai"

    def __init__(
        self,
        *,
        remote_ok: bool = False,
        credential: TokenCredential | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(remote_ok=remote_ok)
        self._tokens = EntraTokens(credential, clock=clock)
        self._refreshes = Refreshes()

    def __repr__(self) -> str:
        return "AzureOpenAIProvider()"

    async def prepare(self, deployment: Deployment) -> None:
        """Fetch the Entra token in a worker thread when the cache has none fresh."""
        if not _entra(deployment):
            return  # an API key or a configured static token: nothing to refresh
        scope = _scope(deployment)
        stale = lambda: not self._tokens.fresh(scope)  # noqa: E731 - a one-line check
        await self._refreshes.ensure(scope, stale, lambda: self._tokens.fetch(scope))

    def translate(self, req: CleanRequest, deployment: Deployment) -> UpstreamRequest:
        """Raises `Untranslatable` for a request that cannot be carried."""
        self._check(req)
        auth = self._auth_headers(deployment)  # after the checks: a refusal needs no token
        # The body model is the deployment name: the v1 path routes on it, classic ignores it.
        return self._request(req, endpoint_url(deployment, req.endpoint), auth, deployment.model)

    def _auth_headers(self, deployment: Deployment) -> dict[str, str]:
        if deployment.auth is AuthMode.API_KEY:
            if not deployment.credential:
                raise ValueError("the deployment has no API key")  # misconfiguration, fail closed
            return {"api-key": deployment.credential}
        if deployment.auth is AuthMode.BEARER_TOKEN:
            # Entra ID: what `prepare` fetched off the loop; without one ready, this raises and
            # nothing is sent. Otherwise the configured credential is a ready-made token.
            entra = _entra(deployment)
            token = self._tokens.ready(_scope(deployment)) if entra else deployment.credential
            return {"Authorization": f"Bearer {token}"}
        raise ValueError(f"azure_openai does not support auth mode {deployment.auth.value}")

    @never_raises_error
    def error(self, response: UpstreamResponse) -> UpstreamError:
        """OpenAI mapping, plus Azure's millisecond `retry-after-ms` on throttling; a 401 or 403
        drops the cached Entra tokens (perhaps revoked or rotated), as Bedrock drops its snapshot.

        A 400 with code `content_filter` already maps to `invalid_request` (a client error that
        costs nothing and is never retried); it is not an upstream fault.
        """
        if response.status in _REVOKED:
            self._tokens.drop()  # the next call's `prepare` fetches a new token off the loop
        mapped = wire.map_error(response, _DEPLOYMENT_FAULTS)
        if mapped.code is not ErrorCode.UPSTREAM_THROTTLED:
            return mapped
        millis = retry_after_ms_seconds(response.headers)
        if millis is None:
            return mapped
        return replace(mapped, retry_after_seconds=millis)
