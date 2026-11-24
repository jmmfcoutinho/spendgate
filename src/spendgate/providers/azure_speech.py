"""Azure Speech adapter: OpenAI `/v1/audio/transcriptions` over Azure fast transcription.

Transcription is priced by audio seconds, which spendgate measures from the request bytes; what the
caller declares is never read. The answer's own `durationMilliseconds` is a second opinion and the
larger of the two is billed, so a short provider figure can never under-bill. Auth is the Speech
resource key (`Ocp-Apim-Subscription-Key`) or an Entra bearer token (lazy `azure-identity`).
"""

import time
from collections.abc import Callable
from http import HTTPStatus
from typing import Protocol

from spendgate.core.audio import AudioError, measure
from spendgate.core.cost import Usage
from spendgate.core.errors import Refusal
from spendgate.providers import azure_speech_shapes as shapes
from spendgate.providers import openai_shapes as openai
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


_REFRESH_MARGIN_SECONDS = 300  # renew this long before expiry: never send a token about to lapse
_ENTRA = "entra"  # one token source per adapter: one refresh key


class AccessToken(Protocol):
    """The shape of `azure.core.credentials.AccessToken`; tests pass a plain fake."""

    @property
    def token(self) -> str: ...

    @property
    def expires_on(self) -> int: ...  # Unix seconds


type TokenSource = Callable[[], AccessToken]


def _default_token() -> TokenSource:
    """Entra token source from `azure-identity` (optional extra): a blocking network call."""
    from azure.identity import DefaultAzureCredential  # noqa: PLC0415 - optional extra

    credential = DefaultAzureCredential()
    return lambda: credential.get_token(shapes.SCOPE)


class AzureSpeech:
    name = "azure_speech"

    def __init__(
        self, token_source: TokenSource | None = None, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._token_source = token_source  # injectable so tests never touch Entra
        self._clock = clock
        self._token: tuple[str, int] | None = None  # (token, expires_on); never printed
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
            await self._refreshes.ensure(_ENTRA, self._stale, self._fetch)

    def _stale(self) -> bool:
        return self._token is None or self._token[1] - self._clock() <= _REFRESH_MARGIN_SECONDS

    def _fetch(self) -> str:
        """A token from the source (blocking: Entra is a network call)."""
        if self._token_source is None:
            self._token_source = _default_token()  # first use only: no Azure SDK until needed
        fetched = self._token_source()
        if not fetched.token:
            raise ValueError("the Entra credential returned an empty token")
        self._token = (fetched.token, fetched.expires_on)
        return fetched.token

    def _bearer(self) -> str:
        """What `prepare` readied, while unexpired; a direct caller without it fetches inline."""
        cached = self._token
        return cached[0] if cached is not None and cached[1] > self._clock() else self._fetch()

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
            return {"Authorization": f"Bearer {self._bearer()}"}
        if deployment.auth is not AuthMode.API_KEY or not deployment.credential:
            raise ValueError("an Azure Speech deployment needs a key or Entra auth")
        return {"Ocp-Apim-Subscription-Key": deployment.credential}
