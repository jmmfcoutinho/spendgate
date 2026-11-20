"""Azure Speech adapter: OpenAI `/v1/audio/transcriptions` over Azure fast transcription.

Transcription is priced by audio seconds, which spendgate measures from the request bytes; what the
caller declares is never read. The answer's own `durationMilliseconds` is a second opinion and the
larger of the two is billed, so a short provider figure can never under-bill. Auth is the Speech
resource key (`Ocp-Apim-Subscription-Key`) or an Entra bearer token (lazy `azure-identity`).
"""

from collections.abc import Callable
from http import HTTPStatus

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

TRANSCRIPTIONS = "/v1/audio/transcriptions"


def _default_token() -> Callable[[], str]:
    """Entra token source from `azure-identity` (optional extra); tokens are cached by the SDK."""
    from azure.identity import DefaultAzureCredential  # noqa: PLC0415 - optional extra

    credential = DefaultAzureCredential()
    return lambda: credential.get_token(shapes.SCOPE).token


class AzureSpeech:
    name = "azure_speech"

    def __init__(self, token_source: Callable[[], str] | None = None) -> None:
        self._token_source = token_source  # injectable so tests never touch Entra

    def validate(self, req: CleanRequest) -> Refusal | None:
        try:
            self._check(req)
        except Untranslatable as refused:
            return refused.refusal
        return None

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
            if self._token_source is None:
                self._token_source = _default_token()  # first use only: no Azure SDK until needed
            return {"Authorization": f"Bearer {self._token_source()}"}
        if deployment.auth is not AuthMode.API_KEY or not deployment.credential:
            raise ValueError("an Azure Speech deployment needs a key or Entra auth")
        return {"Ocp-Apim-Subscription-Key": deployment.credential}
