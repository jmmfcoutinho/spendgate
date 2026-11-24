"""Cloud identity settings: `credential: adc` for Vertex AI."""

from pathlib import Path

import pytest

from spendgate.config import load
from spendgate.config.secrets import CREDENTIAL_HELP, check_credential_ref
from tests.unit.test_config import BASE, ENV, failure, write

GOOGLE = """\
server: {listen: 127.0.0.1:8080, key_pepper: "env:SPENDGATE_KEY_PEPPER"}
ledger: {currency: USD, backend: {kind: sqlite, path: ./l.db}}
providers:
  google: {base_url: "https://vertex.example.test", auth: bearer_token, credential: adc,
           region: europe-west4}
models:
  g: {catalog_id: google/gemini-4-carbon, deployments: [{provider: google}]}
"""


def test_adc_is_accepted_for_vertex_and_resolves_to_no_stored_secret(tmp_path: Path) -> None:
    settings = load(write(tmp_path, GOOGLE), env=ENV)
    built = settings.routes["g"].deployments[0]
    assert built.ambient is True and built.credential is None


@pytest.mark.parametrize(
    ("deployment", "path"),
    [
        ("provider: openai, auth: bearer_token, credential: adc", "models.m.deployments.0"),
        ("provider: openai, credential: adc", "models.m.deployments.0"),  # api_key auth
    ],
)
def test_adc_is_refused_outside_google_vertex(tmp_path: Path, deployment: str, path: str) -> None:
    text = BASE.replace("{provider: openai}", "{" + deployment + "}")
    message = failure(write(tmp_path, text))
    assert f"{path}.credential: adc is for Google Vertex AI only" in message


def test_a_bad_credential_reference_is_refused_without_echoing_it() -> None:
    with pytest.raises(ValueError, match="adc") as info:
        check_credential_ref("sk-pasted-real-key-0000")  # pragma: allowlist secret
    assert "sk-pasted" not in str(info.value) and str(info.value) == CREDENTIAL_HELP
    assert check_credential_ref("file:/run/secrets/x") == "file:/run/secrets/x"
