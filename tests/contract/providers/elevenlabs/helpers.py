"""Shared builders for the ElevenLabs contract tests (fake key, no network)."""

from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import yaml

from spendgate.core.catalog import Catalog, parse
from spendgate.providers.port import AuthMode, CleanRequest, Deployment, Json, UpstreamResponse
from tests.fakes import BASE_URL

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "fake-xi-key-0000"  # pragma: allowlist secret
VOICE = "abcdEFGH1234567890xy"  # the earlier guard's test voice id: 20 alphanumerics
MODEL = "eleven_flash_v2_5"
TEXT = "What have you worked on at Brightline since March 2024?"  # the earlier guard's text
MP3 = b"ID3\x04" + b"\x00" * 64
NOW_MS = int(datetime(2026, 11, 17, tzinfo=UTC).timestamp()) * 1000  # entries in force


def deployment(model: str = MODEL) -> Deployment:
    return Deployment("elevenlabs", BASE_URL, model, AuthMode.API_KEY, credential=KEY)


def request(body: dict[str, Json] | None = None) -> CleanRequest:
    full: dict[str, Json] = {"input": TEXT, "voice": VOICE, **(body or {})}
    return CleanRequest("/v1/audio/speech", "elevenlabs/" + MODEL, full)


def audio(headers: dict[str, str] | None = None, status: int = 200) -> UpstreamResponse:
    merged = {"Content-Type": "audio/mpeg", **(headers or {})}
    return UpstreamResponse(status, merged, MP3)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def error(name: str, status: int, headers: dict[str, str] | None = None) -> UpstreamResponse:
    return UpstreamResponse(status, headers or {}, fixture(name))


def catalog() -> Catalog:
    text = (resources.files("spendgate.catalog") / "prices.yaml").read_text(encoding="utf-8")
    return parse([yaml.safe_load(text)], "0" * 64, ledger_currency="USD")
