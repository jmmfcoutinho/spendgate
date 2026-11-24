"""OpenAI adapter: five endpoints over a bearer key to a config-supplied base URL.

The URL is the configured base plus a fixed path, never anything from the client (SSRF). The body
is rebuilt from the allowlist in `openai_shapes` (through `openai_wire`, shared with Azure); the
pipeline validates the same allowlist before routing (`validate`) and `translate` checks again,
so no path forwards an unchecked body.
"""

from spendgate.providers.openai_wire import OpenAIWire
from spendgate.providers.port import CleanRequest, Deployment, UpstreamRequest


def endpoint_url(base_url: str, endpoint: str) -> str:
    """Config base plus the protocol path; a base that already ends in /v1 is not doubled."""
    return base_url.rstrip("/").removesuffix("/v1") + endpoint


class OpenAIProvider(OpenAIWire):
    """Bodies, relay, usage and errors come from `OpenAIWire`; the URL and bearer key are here."""

    name = "openai"

    async def prepare(self, deployment: Deployment) -> None:
        """A static API key: nothing to refresh."""

    def translate(self, req: CleanRequest, deployment: Deployment) -> UpstreamRequest:
        """Raises `Untranslatable` for a request that cannot be carried."""
        if not deployment.credential:
            raise ValueError("the deployment has no credential")  # misconfiguration, fail closed
        self._check(req)
        auth = {"Authorization": f"Bearer {deployment.credential}"}
        url = endpoint_url(deployment.base_url, req.endpoint)
        return self._request(req, url, auth, deployment.model)
