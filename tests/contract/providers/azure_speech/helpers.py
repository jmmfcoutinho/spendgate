"""Shared builders for the Azure Speech contract tests (fake credentials, no network)."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from importlib import resources
from pathlib import Path

import yaml

from spendgate.core.catalog import Catalog, parse
from spendgate.providers.azure_speech import TRANSCRIPTIONS
from spendgate.providers.port import AuthMode, CleanRequest, Deployment, Json, UpstreamResponse
from tests.fakes import BASE_URL
from tests.unit.audio_builders import wav

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "fake-speech-key-0000"  # pragma: allowlist secret
TOKEN = "fake-entra-token-0000"  # noqa: S105  # pragma: allowlist secret
NOW_MS = int(datetime(2026, 11, 17, tzinfo=UTC).timestamp()) * 1000  # entries in force
# The earlier in-house guard priced Azure Speech at 0.0072 USD/min (fast transcription x 1.20).
REFERENCE_PER_MINUTE = Decimal("0.0072")


def deployment(model: str = "azure-speech", auth: AuthMode = AuthMode.API_KEY) -> Deployment:
    credential = KEY if auth is AuthMode.API_KEY else ""
    return Deployment("azure_speech", BASE_URL, model, auth, credential=credential)


def request(
    seconds: float = 3.0, body: dict[str, Json] | None = None, audio: bytes | None = None
) -> CleanRequest:
    files = {"file": wav(seconds) if audio is None else audio}
    return CleanRequest(TRANSCRIPTIONS, "gw-model", dict(body or {}), files=files)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def answer(name: str, status: int = 200, headers: dict[str, str] | None = None) -> UpstreamResponse:
    return UpstreamResponse(status, headers or {}, fixture(name))


def reply(duration_ms: int | None) -> UpstreamResponse:
    body: dict[str, Json] = {"combinedPhrases": [{"channel": 0, "text": "hello"}], "phrases": []}
    if duration_ms is not None:
        body["durationMilliseconds"] = duration_ms
    return UpstreamResponse(200, {}, json.dumps(body).encode())


def catalog(per_minute: Decimal | None = None) -> Catalog:
    """The bundled catalog; `per_minute` swaps in a rate for the plain Azure Speech entry."""
    text = (resources.files("spendgate.catalog") / "prices.yaml").read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    if per_minute is not None:
        for entry in doc["models"]:
            if entry["id"] in {"azure_speech/azure-speech", "azure_speech/mai-transcribe-2"}:
                entry["meters"] = {"audio_input_per_minute": str(per_minute)}
    return parse([doc], "0" * 64, ledger_currency="USD")


def parts(body: bytes) -> dict[str, tuple[str, bytes]]:
    """Multipart form parts: name -> (Content-Disposition and type lines, raw value bytes)."""
    boundary = body.split(b"\r\n", 1)[0]  # the first line is the opening delimiter
    out: dict[str, tuple[str, bytes]] = {}
    for chunk in body.split(boundary)[1:-1]:
        head, _, value = chunk.removeprefix(b"\r\n").partition(b"\r\n\r\n")
        text = head.decode("latin-1")
        out[text.split('name="')[1].split('"')[0]] = (text, value.removesuffix(b"\r\n"))
    return out
