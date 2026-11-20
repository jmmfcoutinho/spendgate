"""Shared builders for the Google contract tests (fake credentials, no network)."""

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import yaml

from spendgate.core.catalog import Catalog, parse
from spendgate.providers.google import GoogleProvider
from spendgate.providers.port import AuthMode, CleanRequest, Deployment, Json, SseEvent
from spendgate.providers.sse import SseParser
from tests.fakes import BASE_URL

CHAT = "/v1/chat/completions"
FIXTURES = Path(__file__).parent / "fixtures"
# Obviously fake values: never a real key or token.
API_KEY = "fake-gemini-key-0000"  # pragma: allowlist secret
TOKEN = "fake-vertex-token-0000"  # noqa: S105  # pragma: allowlist secret
USER = {"role": "user", "content": "hi"}
NOW_MS = int(datetime(2026, 11, 17, tzinfo=UTC).timestamp()) * 1000  # entry in force to 11-30


class FakeCredentials:
    """Stands in for google-auth service-account credentials: no signing, no network."""

    def __init__(self, token: str = TOKEN, project_id: str | None = "demo-project") -> None:
        self.token: str | None = None
        self.project_id = project_id
        self._next = token
        self.refreshes = 0

    @property
    def valid(self) -> bool:
        return self.token is not None

    def refresh(self, request: object) -> None:
        self.refreshes += 1
        self.token = self._next

    def expire(self) -> None:
        self.token = None


def gemini_deployment(model: str = "gemini-4-carbon") -> Deployment:
    return Deployment("google", BASE_URL, model, AuthMode.API_KEY, credential=API_KEY)


def vertex_deployment(model: str = "gemini-4-carbon", region: str = "europe-west4") -> Deployment:
    # The credential is the service-account JSON in real use; the fake source ignores it.
    return Deployment("google", BASE_URL, model, AuthMode.BEARER_TOKEN, region, credential="{}")


def provider(creds: FakeCredentials | None = None) -> GoogleProvider:
    return GoogleProvider(lambda deployment: creds or FakeCredentials())


def request(body: dict[str, object], *, stream: bool = False, endpoint: str = CHAT) -> CleanRequest:
    full = {"max_tokens": 100} | body  # the pipeline always injects a ceiling
    return CleanRequest(endpoint, "gateway-name", full, stream=stream)  # type: ignore[arg-type]


def converting(body: Mapping[str, object] | None = None, *, stream: bool = False) -> CleanRequest:
    """A request carrying the id, time and model the pipeline would set for the answer."""
    req = request(dict(body or {"messages": [USER]}), stream=stream)
    return replace(req, model="gw-model", response_id="chatcmpl-1", created=7)


def wire(raw: bytes) -> list[SseEvent]:
    """The SSE events a client would parse out of the relay's output bytes."""
    parser = SseParser()
    return [*parser.feed(raw), *parser.close()]


def sent(up_body: bytes) -> dict[str, Json]:
    loaded: dict[str, Json] = json.loads(up_body)
    return loaded


def catalog() -> Catalog:
    text = (resources.files("spendgate.catalog") / "prices.yaml").read_text(encoding="utf-8")
    return parse([yaml.safe_load(text)], "0" * 64, ledger_currency="USD")


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def dig(value: Json, *path: str | int) -> Json:
    """Walk parsed JSON by keys and indexes; a missing step gives None."""
    for key in path:
        if isinstance(key, int) and isinstance(value, list) and -len(value) <= key < len(value):
            value = value[key]
        elif isinstance(key, str) and isinstance(value, dict):
            value = value.get(key)
        else:
            return None
    return value
