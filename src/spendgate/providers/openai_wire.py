"""The OpenAI wire half that `openai` and `azure_openai` share.

Both adapters carry the same bodies: the allowlist and forced fields of `openai_shapes`, the usage
readers and error mapping of `openai_usage`, and the hidden usage-only stream chunk. Only the URL
and the auth headers differ, so each adapter subclasses `OpenAIWire` and writes `translate` (and
`prepare`, and any error refinement) itself. One copy means a fix to a reader lands in both.
"""

from collections.abc import Mapping
from http import HTTPStatus

from spendgate.core.cost import Usage
from spendgate.core.errors import Refusal
from spendgate.providers import openai_shapes as shapes
from spendgate.providers import openai_usage as wire
from spendgate.providers.port import (
    CleanRequest,
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


class OpenAIWire:
    """Everything of an OpenAI-shaped adapter but its URL and auth."""

    def __init__(self, *, remote_ok: bool = False) -> None:
        self._remote_ok = remote_ok  # config: may image parts be remote URLs (provider fetches)

    def validate(self, req: CleanRequest) -> Refusal | None:
        return shapes.validate(req.endpoint, req.body, req.files, remote_ok=self._remote_ok)

    def _check(self, req: CleanRequest) -> None:
        """`translate` checks the allowlist again; raises `Untranslatable`."""
        shapes.check(req.endpoint, req.body, req.files, remote_ok=self._remote_ok)

    def _request(
        self, req: CleanRequest, url: str, auth: Mapping[str, str], model: str
    ) -> UpstreamRequest:
        """The rebuilt body for `model` with `auth` headers, POSTed to `url`."""
        content_type, body = shapes.encode(req, model)
        headers = {
            **auth,
            "Content-Type": content_type,
            "Accept": "text/event-stream" if req.stream else "application/json",
        }
        return UpstreamRequest("POST", url, headers, body, stream=req.stream)

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
            ok = HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES
            return shapes.speech_usage(req.body) if ok else None
        return wire.usage_of(response, req.endpoint)

    @never_raises_error
    def error(self, response: UpstreamResponse) -> UpstreamError:
        return wire.map_error(response)
