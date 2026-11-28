"""`credential: entra`: the host's Entra ID identity for the Azure adapters (ADR-0035, issue #141).

From the YAML through the router to the adapter, with `DefaultAzureCredential` replaced by a fake:
no network. A static bearer token for Azure OpenAI keeps working; a stored credential that Azure
Speech would ignore is refused at load.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest

from spendgate.cli import serve
from spendgate.cli.client import EXIT_CONFIG
from spendgate.config import load
from spendgate.config.secrets import CREDENTIAL_HELP, check_credential_ref
from spendgate.pipeline.candidates import Router
from spendgate.providers import azure_openai_auth
from spendgate.providers.azure_openai import AzureOpenAIProvider
from spendgate.providers.azure_openai_auth import V1_SCOPE
from spendgate.providers.azure_speech import AzureSpeech
from spendgate.providers.azure_speech_shapes import SCOPE as SPEECH_SCOPE
from spendgate.providers.port import CleanRequest, Deployment
from tests.contract.providers.azure_speech.helpers import request as speech_request
from tests.unit.test_config import ENV, failure, write

pytestmark = pytest.mark.anyio

TOKEN = "fake-entra-token-0000"  # noqa: S105  # pragma: allowlist secret
STATIC = "fake-static-bearer-0000"  # pragma: allowlist secret
CONFIG = """\
server: {{listen: 127.0.0.1:8080, key_pepper: "env:SPENDGATE_KEY_PEPPER"}}
ledger: {{currency: USD, backend: {{kind: sqlite, path: ./l.db}}}}
providers:
  azure_openai: {{base_url: "https://res.openai.azure.com", auth: bearer_token{oai}}}
  azure_speech: {{base_url: "https://res.cognitiveservices.azure.com", auth: bearer_token{speech}}}
models:
  luna: {{catalog_id: azure_openai/gpt-6-luna, deployments: [{{provider: azure_openai}}]}}
  stt: {{catalog_id: azure_speech/azure-speech, deployments: [{{provider: azure_speech}}]}}
"""
CHAT = CleanRequest(
    "/v1/chat/completions",
    "luna",
    {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
    output_ceiling=5,
)
ENTRA = ", credential: entra"


def config(oai: str = ENTRA, speech: str = ENTRA) -> str:
    return CONFIG.format(oai=oai, speech=speech)


@dataclass(frozen=True)
class Token:
    token: str
    expires_on: int


class FakeDefault:
    """Stands in for `DefaultAzureCredential`; records the scopes it was asked for."""

    def __init__(self) -> None:
        self.scopes: list[tuple[str, ...]] = []

    def get_token(self, *scopes: str) -> Token:
        self.scopes.append(scopes)
        return Token(TOKEN, 2**40)


@pytest.fixture
def default(monkeypatch: pytest.MonkeyPatch) -> FakeDefault:
    fake = FakeDefault()
    monkeypatch.setattr(azure_openai_auth, "default_credential", lambda: fake)
    return fake


def routed(tmp_path: Path, text: str, alias: str) -> Deployment:
    """The deployment the gateway hands the adapter for `alias`, built as `serve` builds it."""
    settings = load(write(tmp_path, text), env=ENV | {"AZURE_BEARER": STATIC})
    return Router(settings.routes, settings.health).targets(alias)[0].deployment


async def test_an_azure_openai_bearer_token_deployment_with_credential_entra_asks_default_azure_credential(  # noqa: E501
    tmp_path: Path, default: FakeDefault
) -> None:
    dep = routed(tmp_path, config(), "luna")
    assert dep.ambient and dep.credential == ""
    provider = AzureOpenAIProvider()  # as the gateway builds it: no injected credential
    await provider.prepare(dep)
    sent = provider.translate(CHAT, dep)
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
    assert default.scopes == [(V1_SCOPE,)]


async def test_an_azure_speech_bearer_token_deployment_with_credential_entra_asks_it_too(
    tmp_path: Path, default: FakeDefault
) -> None:
    dep = routed(tmp_path, config(), "stt")
    adapter = AzureSpeech()
    await adapter.prepare(dep)
    assert adapter.translate(speech_request(), dep).headers["Authorization"] == f"Bearer {TOKEN}"
    assert default.scopes == [(SPEECH_SCOPE,)]


async def test_a_static_bearer_token_for_azure_openai_is_unchanged(
    tmp_path: Path, default: FakeDefault
) -> None:
    dep = routed(tmp_path, config(oai=', credential: "env:AZURE_BEARER"'), "luna")
    assert not dep.ambient
    provider = AzureOpenAIProvider()
    await provider.prepare(dep)  # nothing to fetch
    assert provider.translate(CHAT, dep).headers["Authorization"] == f"Bearer {STATIC}"
    assert default.scopes == []  # Entra is never asked


@pytest.mark.parametrize("where", ["provider", "deployment"])
def test_an_azure_speech_bearer_token_deployment_with_a_credential_that_would_be_ignored_is_refused_at_load(  # noqa: E501
    tmp_path: Path, where: str
) -> None:
    text = config(speech=', credential: "env:AZURE_BEARER"')
    path = "providers.azure_speech.credential"
    if where == "deployment":
        text = config().replace(
            "[{provider: azure_speech}]", '[{provider: azure_speech, credential: "env:X"}]'
        )
        path = "models.stt.deployments.0.credential"
    message = failure(write(tmp_path, text), env=ENV | {"AZURE_BEARER": STATIC, "X": STATIC})
    assert f"{path}: azure_speech with auth bearer_token signs in with Entra ID" in message
    assert "credential: entra" in message
    assert STATIC not in message


def test_check_config_and_serve_exit_3_for_an_ignored_speech_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in (ENV | {"AZURE_BEARER": STATIC}).items():
        monkeypatch.setenv(name, value)
    path = str(write(tmp_path, config(speech=', credential: "env:AZURE_BEARER"')))
    assert serve.run_check_config(path) == EXIT_CONFIG == 3
    assert serve.load_settings(path) == EXIT_CONFIG
    assert "providers.azure_speech.credential" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("oai", "speech", "path"),
    [
        (", auth: api_key, credential: entra", ENTRA, "providers.azure_openai.credential"),
        (ENTRA, ", auth: api_key, credential: entra", "providers.azure_speech.credential"),
    ],
)
def test_entra_is_refused_with_api_key_auth(
    tmp_path: Path, oai: str, speech: str, path: str
) -> None:
    text = config(oai, speech).replace("auth: bearer_token, auth: api_key", "auth: api_key")
    message = failure(write(tmp_path, text))
    assert f"{path}: entra is for azure_openai and azure_speech only (auth bearer_token)" in message


def test_entra_is_refused_for_other_providers(tmp_path: Path) -> None:
    text = (
        config()
        .replace(
            "[{provider: azure_openai}]",
            "[{provider: openai, auth: bearer_token, credential: entra}]",
        )
        .replace("azure_openai/gpt-6-luna", "openai/gpt-6-luna")
    )
    text = text.replace(
        "  azure_openai:", '  openai: {base_url: "https://api.openai.com/v1"}\n  azure_openai:'
    )
    message = failure(write(tmp_path, text))
    assert "models.luna.deployments.0.credential: entra is for azure_openai" in message


def test_a_missing_azure_bearer_credential_names_entra(tmp_path: Path) -> None:
    message = failure(write(tmp_path, config(oai="")))
    assert "auth bearer_token needs a credential (entra for the host's Entra ID identity" in message


def test_entra_is_a_credential_marker_and_the_help_names_it() -> None:
    assert check_credential_ref("entra") == "entra"
    assert "'entra'" in CREDENTIAL_HELP
