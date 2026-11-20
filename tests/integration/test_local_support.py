"""Shared rig for the library-mode tests (no tests here): config on disk, fake upstream, clock."""

from pathlib import Path

import httpx

from spendgate.config import Settings, load
from tests.fakes import FakeUpstream
from tests.integration.harness import CATALOG_OVERRIDE, CONFIG, ENV, MODELS, NOW_MS

CHAT = "/v1/chat/completions"
BODY = {"model": "alpha", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}


def settings_in(tmp_path: Path, models: str = MODELS, attempts: int = 2) -> Settings:
    """The gateway test config (two `alpha` deployments) loaded from `tmp_path`."""
    (tmp_path / "catalog_override.yaml").write_text(CATALOG_OVERRIDE)
    path = tmp_path / "spendgate.yaml"
    path.write_text(
        CONFIG.format(
            max_body=26_214_400,
            max_json_body=4_194_304,
            concurrent=128,
            attempts=attempts,
            models=models,
            cooldown=5,
        )
    )
    return load(path, env=ENV)


def now() -> int:
    return NOW_MS


def fake_client(fake: FakeUpstream) -> httpx.AsyncClient:
    return fake.client()


def error_code(response: httpx.Response) -> str:
    body = response.json()
    assert isinstance(body, dict) and isinstance(body["error"], dict)
    code = body["error"]["code"]
    assert isinstance(code, str)
    return code
