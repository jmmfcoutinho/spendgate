"""Azure Speech adapter: OpenAI `/v1/audio/transcriptions` over Azure fast transcription.

Transcription is priced by audio seconds, which spendgate measures from the request bytes; what the
caller declares is never read. The answer's own `durationMilliseconds` is a second opinion and the
larger of the two is billed, so a short provider figure can never under-bill. Auth is the Speech
resource key (`Ocp-Apim-Subscription-Key`) or an Entra bearer token (lazy `azure-identity`, cached
by `azure_openai_auth.EntraTokens`, fetched only in `prepare`, dropped on a 401 or 403).
"""

import time
from collections.abc import Callable
from http import HTTPStatus

from spendgate.core.audio import AudioError, measure
from spendgate.core.cost import Usage
from spendgate.core.errors import Refusal
from spendgate.providers import azure_speech_shapes as shapes
from spendgate.providers import openai_shapes as openai
from spendgate.providers.azure_openai_auth import AccessToken, EntraTokens
from spendgate.providers.port import (
    AuthMode,
    CleanRequest,
    Deployment,
    SseLimits,
    StreamRelay,
    Untranslatable,
    UpstreamError,
    UpstreamRequest,
    UpstreamResponse,
    never_raises_error,
    never_raises_response,
    never_raises_usage,
)
from spendgate.providers.refresh import Refreshes

TRANSCRIPTIONS = "/v1/audio/transcriptions"
_REVOKED = (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN)  # perhaps a revoked or stale token

type TokenSource = Callable[[], AccessToken]


class _SourceCredential:
    """A zero-argument token source seen as a `TokenCredential` (the scope is fixed here)."""

    def __init__(self, source: TokenSource) -> None:
        self._source = source

    def get_token(self, *scopes: str) -> AccessToken:
        return self._source()


class AzureSpeech:
    name = "azure_speech"

    def __init__(
        self, token_source: TokenSource | None = None, *, clock: Callable[[], float] = time.time
    ) -> None:
        # Injectable so tests never touch Entra; None asks `DefaultAzureCredential` on first use.
        credential = None if token_source is None else _SourceCredential(token_source)
        self._tokens = EntraTokens(credential, clock=clock)
        self._refreshes = Refreshes()

    def __repr__(self) -> str:
        return "AzureSpeech()"

    def validate(self, req: CleanRequest) -> Refusal | None:
        try:
            self._check(req)
        except Untranslatable as refused:
            return refused.refusal
        return None

    async def prepare(self, deployment: Deployment) -> None:
        """Entra: fetch the token in a worker thread when none is cached outside the margin."""
        if deployment.auth is AuthMode.BEARER_TOKEN:
            stale = lambda: not self._tokens.fresh(shapes.SCOPE)  # noqa: E731 - a one-line check
            await self._refreshes.ensure(
                shapes.SCOPE, stale, lambda: self._tokens.fetch(shapes.SCOPE)
            )

    def translate(self, req: CleanRequest, deployment: Deployment) -> UpstreamRequest:
        """Raises `Untranslatable` for a request Azure cannot carry."""
        audio = self._check(req)
        version = deployment.api_version or shapes.API_VERSION
        if not shapes.VERSION_FORMAT.fullmatch(version):
            raise ValueError("the deployment api_version is malformed")  # config error, fail closed
        definition = shapes.definition(deployment.model, req.body.get("language"))
        content_type, body = shapes.multipart(definition, audio)
        headers = {**self._auth(deployment), "Content-Type": content_type}
        url = f"{deployment.base_url.rstrip('/')}{shapes.PATH}?api-version={version}"
        return UpstreamRequest("POST", url, headers, body)

    @never_raises_response
    def response(self, req: CleanRequest, resp: UpstreamResponse) -> UpstreamResponse:
        if not HTTPStatus.OK <= resp.status < HTTPStatus.MULTIPLE_CHOICES:
            return resp  # errors are mapped by `error`, not reshaped
        return shapes.convert(req.body, resp)

    def relay(self, req: CleanRequest, limits: SseLimits = SseLimits()) -> StreamRelay:  # noqa: B008
        raise Untranslatable("Azure Speech transcription does not stream", "stream")

    @never_raises_usage
    def usage(self, req: CleanRequest, response: UpstreamResponse) -> Usage | None:
        """The larger of the measured and the reported duration; None leaves the hold charged."""
        if not HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES:
            return None
        reported = shapes.reported_ms(response.body)
        if reported is None:
            return None  # unreadable answer: the full hold stands, as in the earlier guard
        try:
            measured = measure(req.files.get("file", b"")).duration_ms
        except AudioError:
            return None
        return Usage(audio_seconds=shapes.seconds(max(measured, reported)))

    @never_raises_error
    def error(self, response: UpstreamResponse) -> UpstreamError:
        if response.status in _REVOKED:
            self._tokens.drop()  # perhaps revoked or rotated: the next `prepare` fetches anew
        return shapes.map_error(response)

    def _check(self, req: CleanRequest) -> bytes:
        if req.endpoint != TRANSCRIPTIONS:
            raise Untranslatable("endpoint is not served by azure_speech", "endpoint")
        if req.stream:
            raise Untranslatable("Azure Speech transcription does not stream", "stream")
        openai.check(req.endpoint, req.body, req.files)  # the shared allowlist: fields, one file
        return shapes.check(req.body, req.files)

    def _auth(self, deployment: Deployment) -> dict[str, str]:
        if deployment.auth is AuthMode.BEARER_TOKEN:
            if deployment.credential:  # config refuses it too: a stored token is never used
                raise ValueError("Azure Speech bearer auth is Entra ID only (credential: entra)")
            # What `prepare` readied; without it this raises (nothing sent), never fetches inline.
            return {"Authorization": f"Bearer {self._tokens.ready(shapes.SCOPE)}"}
        if deployment.auth is not AuthMode.API_KEY or not deployment.credential:
            raise ValueError("an Azure Speech deployment needs a key or Entra auth")
        return {"Ocp-Apim-Subscription-Key": deployment.credential}
