"""OpenAI adapter: five endpoints over a bearer key to a config-supplied base URL.

The URL is the configured base plus a fixed path, never anything from the client (SSRF). The body
is rebuilt from the allowlist in `openai_shapes`; the pipeline validates the same allowlist before
routing (`validate`) and `translate` checks again, so no path forwards an unchecked body.
"""

from http import HTTPStatus

from spendgate.core.cost import Usage
from spendgate.core.errors import Refusal
from spendgate.providers import openai_shapes as shapes
from spendgate.providers import openai_usage as wire
from spendgate.providers.port import (
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
)
from spendgate.providers.sse import SseRelay


def endpoint_url(base_url: str, endpoint: str) -> str:
    """Config base plus the protocol path; a base that already ends in /v1 is not doubled."""
    return base_url.rstrip("/").removesuffix("/v1") + endpoint


class OpenAIProvider:
    name = "openai"

    def __init__(self, *, remote_ok: bool = False) -> None:
        self._remote_ok = remote_ok  # config: may image parts be remote URLs (provider fetches)

    def validate(self, req: CleanRequest) -> Refusal | None:
        return shapes.validate(req.endpoint, req.body, req.files, remote_ok=self._remote_ok)

    def translate(self, req: CleanRequest, deployment: Deployment) -> UpstreamRequest:
        """Raises `Untranslatable` for a request that cannot be carried."""
        if not deployment.credential:
            raise ValueError("the deployment has no credential")  # misconfiguration, fail closed
        shapes.check(req.endpoint, req.body, req.files, remote_ok=self._remote_ok)
        content_type, body = shapes.encode(req, deployment.model)
        headers = {
            "Authorization": f"Bearer {deployment.credential}",
            "Content-Type": content_type,
            "Accept": "text/event-stream" if req.stream else "application/json",
        }
        url = endpoint_url(deployment.base_url, req.endpoint)
        return UpstreamRequest("POST", url, headers, body, stream=req.stream)

    @never_raises_response
    def response(self, req: CleanRequest, resp: UpstreamResponse) -> UpstreamResponse:
        return resp  # already the client's shape

    def relay(self, req: CleanRequest, limits: SseLimits = SseLimits()) -> StreamRelay:  # noqa: B008
        """Relays events unchanged, minus the usage-only chunk when `translate` injected it."""
        injected = shapes.injected_include_usage(req.endpoint, req.body)
        hide = wire.is_usage_only_chunk if injected else None
        return SseRelay(wire.OpenAIStreamUsage(), limits, hide)

    @never_raises_usage
    def usage(self, req: CleanRequest, response: UpstreamResponse) -> Usage | None:
        if req.endpoint == shapes.SPEECH:  # audio bytes carry no usage: billed by input characters
            ok = HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES
            return shapes.speech_usage(req.body) if ok else None
        return wire.usage_of(response)

    @never_raises_error
    def error(self, response: UpstreamResponse) -> UpstreamError:
        return wire.map_error(response)
