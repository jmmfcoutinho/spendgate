"""Shared helpers for the Azure OpenAI adapter contract tests."""

import json
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from spendgate.providers.azure_openai import AzureOpenAIProvider
from spendgate.providers.port import AuthMode, Deployment, Json, UpstreamResponse

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "azure-key-not-a-real-key"  # noqa: S105  # pragma: allowlist secret
TOKEN = "entra-token-not-real"  # noqa: S105  # pragma: allowlist secret
BASE = "https://res.openai.azure.com"
VERSION = "2025-04-01-preview"


def fixture(name: str) -> Json:
    data: Json = json.loads((FIXTURES / name).read_text())
    return data


def answer(name: str, status: int = 200, headers: dict[str, str] | None = None) -> UpstreamResponse:
    return UpstreamResponse(status, headers or {}, (FIXTURES / name).read_bytes())


@dataclass
class FakeToken:
    token: str
    expires_on: int


class FakeCredential:
    """Stands in for an azure-identity credential: no network, counts calls."""

    def __init__(self, lifetime: int = 3600, now: float | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.now = time.time() if now is None else now
        self.lifetime = lifetime

    def get_token(self, *scopes: str) -> FakeToken:
        self.calls.append(scopes)
        return FakeToken(f"{TOKEN}-{len(self.calls)}", int(self.now) + self.lifetime)


def deployment(
    model: str = "prod-alpha",
    *,
    api_version: str | None = None,
    auth: AuthMode = AuthMode.API_KEY,
    credential: str = SECRET,
) -> Deployment:
    return Deployment(
        "azure_openai", BASE, model, auth, api_version=api_version, credential=credential
    )


@pytest.fixture
def provider() -> AzureOpenAIProvider:
    return AzureOpenAIProvider()
