"""Shared helpers for the OpenAI adapter contract tests."""

import json
from pathlib import Path

import pytest

from spendgate.providers.openai import OpenAIProvider
from spendgate.providers.port import AuthMode, Deployment, Json, UpstreamResponse

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "sk-test-not-a-real-key"  # noqa: S105  # pragma: allowlist secret


def fixture(name: str) -> Json:
    data: Json = json.loads((FIXTURES / name).read_text())
    return data


def answer(name: str, status: int = 200, headers: dict[str, str] | None = None) -> UpstreamResponse:
    return UpstreamResponse(status, headers or {}, (FIXTURES / name).read_bytes())


@pytest.fixture
def provider() -> OpenAIProvider:
    return OpenAIProvider()


@pytest.fixture
def deployment() -> Deployment:
    return Deployment(
        "openai", "https://upstream.test", "gpt-upstream", AuthMode.API_KEY, credential=SECRET
    )
