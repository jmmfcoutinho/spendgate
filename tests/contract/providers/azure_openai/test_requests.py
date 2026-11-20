"""Request side: URL rules, deployment naming, auth modes, forced fields, secrecy."""

import json
import logging

import pytest

from spendgate.providers import openai_shapes as shapes
from spendgate.providers.azure_openai import AzureOpenAIProvider, resource_root
from spendgate.providers.azure_openai_auth import CLASSIC_SCOPE, V1_SCOPE, EntraTokens
from spendgate.providers.port import AuthMode, CleanRequest, Json, Untranslatable
from tests.contract.providers.azure_openai.conftest import (
    BASE,
    SECRET,
    TOKEN,
    VERSION,
    FakeCredential,
    deployment,
)
from tests.unit.audio_builders import wav

MSG: Json = [{"role": "user", "content": "hi"}]


def chat(**extra: Json) -> CleanRequest:
    body: dict[str, Json] = {"model": "alpha", "messages": MSG, **extra}
    return CleanRequest(shapes.CHAT, "alpha", body, stream=body.get("stream") is True)


def test_classic_url_uses_the_deployment_name_not_the_alias(provider: AzureOpenAIProvider) -> None:
    sent = provider.translate(chat(), deployment("prod-alpha", api_version=VERSION))
    assert (
        sent.url == f"{BASE}/openai/deployments/prod-alpha/chat/completions?api-version={VERSION}"
    )
    assert json.loads(sent.body)["model"] == "prod-alpha"  # never the client's alias


def test_classic_url_per_endpoint(provider: AzureOpenAIProvider) -> None:
    dep = deployment("d1", api_version=VERSION)
    cases = {
        shapes.EMBEDDINGS: ({"input": "x"}, f"{BASE}/openai/deployments/d1/embeddings"),
        shapes.SPEECH: (
            {"input": "x", "voice": "alloy"},
            f"{BASE}/openai/deployments/d1/audio/speech",
        ),
        shapes.RESPONSES: ({"input": "x"}, f"{BASE}/openai/responses"),  # model in the body
    }
    for endpoint, (body, url) in cases.items():
        sent = provider.translate(CleanRequest(endpoint, "m", body), dep)
        assert sent.url == f"{url}?api-version={VERSION}"
    audio = CleanRequest(shapes.TRANSCRIPTIONS, "m", {}, files={"file": wav(1)})
    sent = provider.translate(audio, dep)
    assert sent.url == f"{BASE}/openai/deployments/d1/audio/transcriptions?api-version={VERSION}"
    assert sent.headers["Content-Type"].startswith("multipart/form-data")


def test_v1_url_when_no_api_version(provider: AzureOpenAIProvider) -> None:
    sent = provider.translate(chat(), deployment("prod-alpha"))
    assert sent.url == f"{BASE}/openai/v1/chat/completions"  # no query, no deployment in the path
    assert json.loads(sent.body)["model"] == "prod-alpha"
    resp = provider.translate(CleanRequest(shapes.RESPONSES, "m", {"input": "x"}), deployment("d"))
    assert resp.url == f"{BASE}/openai/v1/responses"


@pytest.mark.parametrize(
    "base", [BASE, f"{BASE}/", f"{BASE}/openai", f"{BASE}/openai/v1", f"{BASE}/openai/v1/"]
)
def test_the_base_may_already_carry_openai_prefixes(base: str) -> None:
    assert resource_root(base) == BASE


def test_a_deployment_name_cannot_inject_path_or_query(provider: AzureOpenAIProvider) -> None:
    sent = provider.translate(chat(), deployment("a/../b?x=1", api_version=VERSION))
    assert "/deployments/a%2F..%2Fb%3Fx%3D1/chat/completions?api-version=" in sent.url


def test_api_key_goes_in_the_api_key_header_never_the_url(provider: AzureOpenAIProvider) -> None:
    sent = provider.translate(chat(), deployment(api_version=VERSION))
    assert sent.headers["api-key"] == SECRET
    assert "Authorization" not in sent.headers
    assert SECRET not in sent.url


def test_entra_token_is_a_bearer_header_and_is_cached() -> None:
    cred = FakeCredential()
    provider = AzureOpenAIProvider(credential=cred)
    dep = deployment(auth=AuthMode.BEARER_TOKEN, credential="", api_version=VERSION)
    first = provider.translate(chat(), dep)
    second = provider.translate(chat(), dep)
    assert first.headers["Authorization"] == f"Bearer {TOKEN}-1" == second.headers["Authorization"]
    assert "api-key" not in first.headers
    assert cred.calls == [(CLASSIC_SCOPE,)]  # one fetch for two requests


def test_entra_scope_follows_the_url_style() -> None:
    cred = FakeCredential()
    AzureOpenAIProvider(credential=cred).translate(
        chat(), deployment(auth=AuthMode.BEARER_TOKEN, credential="")
    )
    assert cred.calls == [(V1_SCOPE,)]


def test_a_token_is_refreshed_near_expiry() -> None:
    cred = FakeCredential(lifetime=3600, now=1000)
    clock = {"now": 1000.0}
    tokens = EntraTokens(cred, clock=lambda: clock["now"])
    assert tokens.bearer("s") == f"{TOKEN}-1"
    clock["now"] = 1000 + 3600 - 301  # still outside the 5 minute margin
    assert tokens.bearer("s") == f"{TOKEN}-1"
    clock["now"] = 1000 + 3600 - 299
    assert tokens.bearer("s") == f"{TOKEN}-2"
    assert len(cred.calls) == 2


def test_a_configured_credential_is_used_as_a_ready_bearer_token(
    provider: AzureOpenAIProvider,
) -> None:
    dep = deployment(auth=AuthMode.BEARER_TOKEN, credential="static-token")
    assert provider.translate(chat(), dep).headers["Authorization"] == "Bearer static-token"


def test_misconfigured_auth_fails_closed(provider: AzureOpenAIProvider) -> None:
    with pytest.raises(ValueError, match="no API key"):
        provider.translate(chat(), deployment(credential=""))
    with pytest.raises(ValueError, match="sigv4"):
        provider.translate(chat(), deployment(auth=AuthMode.SIGV4))


def test_a_refused_request_needs_no_token() -> None:
    cred = FakeCredential()
    provider = AzureOpenAIProvider(credential=cred)
    dep = deployment(auth=AuthMode.BEARER_TOKEN, credential="")
    with pytest.raises(Untranslatable):
        provider.translate(chat(service_tier="flex"), dep)
    assert cred.calls == []


def test_same_allowlist_forced_fields_and_refused_price_options(
    provider: AzureOpenAIProvider,
) -> None:
    dep = deployment(api_version=VERSION)
    extras: list[dict[str, Json]] = [{"service_tier": "flex"}, {"prediction": {}}, {"surprise": 1}]
    for extra in extras:
        with pytest.raises(Untranslatable):
            provider.translate(chat(**extra), dep)
        assert provider.validate(chat(**extra)) is not None
    body = json.loads(provider.translate(chat(stream=True), dep).body)
    assert body["store"] is False
    assert body["stream_options"] == {"include_usage": True}
    resp = provider.translate(CleanRequest(shapes.RESPONSES, "m", {"input": "x"}), dep)
    assert json.loads(resp.body)["store"] is False
    assert provider.validate(CleanRequest("/v1/messages", "m", {})) is not None


def test_secrets_stay_out_of_reprs_and_logs(
    provider: AzureOpenAIProvider, caplog: pytest.LogCaptureFixture
) -> None:
    cred = FakeCredential()
    entra = AzureOpenAIProvider(credential=cred)
    with caplog.at_level(logging.DEBUG):
        sent = provider.translate(chat(), deployment(api_version=VERSION))
        bearer = entra.translate(chat(), deployment(auth=AuthMode.BEARER_TOKEN, credential=""))
    text = repr(sent) + repr(bearer) + repr(deployment()) + repr(entra) + repr(entra._tokens)
    assert SECRET not in text
    assert TOKEN not in text
    assert "hi" not in repr(sent)  # user content
    assert not caplog.records
