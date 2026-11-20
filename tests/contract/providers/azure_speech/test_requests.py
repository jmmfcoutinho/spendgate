"""Request side: validation, translation to fast transcription, auth, secrecy."""

import json
import logging
from collections.abc import Callable

import pytest

from spendgate.providers import azure_speech
from spendgate.providers.azure_speech import AzureSpeech
from spendgate.providers.port import AuthMode, CleanRequest, Json, Untranslatable
from tests.contract.providers.azure_speech.helpers import (
    KEY,
    TOKEN,
    deployment,
    parts,
    request,
)
from tests.fakes import BASE_URL
from tests.unit.audio_builders import mp3, wav


def sent_definition(body: bytes) -> dict[str, Json]:
    loaded: dict[str, Json] = json.loads(parts(body)["definition"][1].decode())
    return loaded


def test_translate_builds_the_fast_transcription_request_with_the_subscription_key() -> None:
    out = AzureSpeech().translate(request(), deployment())
    assert out.method == "POST"
    assert out.url == f"{BASE_URL}/speechtotext/transcriptions:transcribe?api-version=2025-10-15"
    assert out.headers["Ocp-Apim-Subscription-Key"] == KEY
    assert "Authorization" not in out.headers  # a bearer header would be read as an Entra token
    assert out.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert not out.stream


def test_the_audio_part_is_named_audio_with_an_extension_from_the_bytes() -> None:
    audio = mp3(10)
    out = AzureSpeech().translate(request(audio=audio), deployment())
    disposition, value = parts(out.body)["audio"]
    assert 'filename="audio.mp3"' in disposition
    assert value == audio  # the bytes are forwarded unchanged


def test_the_definition_is_built_by_the_gateway_for_the_plain_model() -> None:
    out = AzureSpeech().translate(request(body={"language": "pt"}), deployment())
    # Profanity masking would corrupt CV words; the locale comes from the ISO 639-1 language.
    assert sent_definition(out.body) == {"profanityFilterMode": "None", "locales": ["pt-PT"]}
    assert sent_definition(AzureSpeech().translate(request(), deployment()).body) == {
        "profanityFilterMode": "None"
    }  # no language: Azure detects it


def test_mai_adds_enhanced_mode_and_a_bare_language_code() -> None:
    out = AzureSpeech().translate(request(body={"language": "pt"}), deployment("mai-transcribe-2"))
    assert sent_definition(out.body) == {
        "enhancedMode": {"enabled": True, "model": "MAI-Transcribe-2"},
        "locales": ["pt"],
    }


def test_nothing_else_the_caller_sends_reaches_azure() -> None:
    body: dict[str, Json] = {"model": "x", "response_format": "verbose_json", "language": "en"}
    out = AzureSpeech().translate(request(body=body), deployment())
    assert set(parts(out.body)) == {"definition", "audio"}


def test_api_version_comes_from_the_deployment_and_must_be_well_formed() -> None:
    dep = deployment()
    newer = type(dep)(dep.provider, dep.base_url, dep.model, dep.auth, api_version="2026-01-01",
                      credential=KEY)  # fmt: skip
    assert AzureSpeech().translate(request(), newer).url.endswith("?api-version=2026-01-01")
    bad = type(dep)(dep.provider, dep.base_url, dep.model, dep.auth, api_version="1&x=2",
                    credential=KEY)  # fmt: skip
    with pytest.raises(ValueError, match="api_version"):
        AzureSpeech().translate(request(), bad)


def test_entra_bearer_token_is_used_when_the_deployment_asks_for_it() -> None:
    calls: list[int] = []

    def source() -> str:
        calls.append(1)
        return TOKEN

    adapter = AzureSpeech(source)
    out = adapter.translate(request(), deployment(auth=AuthMode.BEARER_TOKEN))
    assert out.headers["Authorization"] == f"Bearer {TOKEN}"
    assert "Ocp-Apim-Subscription-Key" not in out.headers
    assert TOKEN not in out.url
    adapter.translate(request(), deployment(auth=AuthMode.BEARER_TOKEN))
    assert len(calls) == 2  # asked per request: the SDK caches and refreshes, we never do


def test_azure_identity_is_loaded_lazily_and_only_for_entra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[int] = []

    def fake_default() -> Callable[[], str]:
        loads.append(1)
        return lambda: TOKEN

    monkeypatch.setattr(azure_speech, "_default_token", fake_default)
    adapter = AzureSpeech()
    adapter.translate(request(), deployment())  # a key deployment never loads the SDK
    assert not loads
    adapter.translate(request(), deployment(auth=AuthMode.BEARER_TOKEN))
    adapter.translate(request(), deployment(auth=AuthMode.BEARER_TOKEN))
    assert loads == [1]


def test_a_deployment_without_a_credential_fails_closed() -> None:
    bare = type(deployment())("azure_speech", BASE_URL, "azure-speech", AuthMode.API_KEY)
    with pytest.raises(ValueError, match="key or Entra"):
        AzureSpeech().translate(request(), bare)
    sigv4 = type(deployment())("azure_speech", BASE_URL, "azure-speech", AuthMode.SIGV4)
    with pytest.raises(ValueError, match="key or Entra"):
        AzureSpeech().translate(request(), sigv4)


def test_secrets_stay_out_of_reprs_urls_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    out = AzureSpeech().translate(request(), deployment())
    assert KEY not in out.url
    assert KEY not in repr(out)
    assert KEY not in repr(deployment())
    assert KEY not in caplog.text


BAD_FIELDS: list[tuple[dict[str, Json], str]] = [
    ({"prompt": "names"}, "prompt"),
    ({"temperature": 0.2}, "temperature"),
    ({"response_format": "srt"}, "response_format"),
    ({"response_format": "vtt"}, "response_format"),
    ({"language": "xx"}, "language"),
    ({"language": "pt-PT"}, "language"),
    ({"stream": True}, "stream"),
    ({"chunking_strategy": "auto"}, "chunking_strategy"),
]


@pytest.mark.parametrize(("body", "param"), BAD_FIELDS)
def test_what_azure_cannot_carry_is_refused_not_dropped(body: dict[str, Json], param: str) -> None:
    refusal = AzureSpeech().validate(request(body=body))
    assert refusal is not None
    assert refusal.param == param
    with pytest.raises(Untranslatable):
        AzureSpeech().translate(request(body=body), deployment())


@pytest.mark.parametrize(
    "audio", [b"", b"not audio at all, just some text", wav(3.0)[:30], b"RIFF" + b"\x00" * 40]
)
def test_unmeasurable_audio_is_refused_before_routing(audio: bytes) -> None:
    refusal = AzureSpeech().validate(request(audio=audio))
    assert refusal is not None
    assert refusal.param == "file"


def test_the_file_part_must_be_exactly_one_file() -> None:
    two = CleanRequest("/v1/audio/transcriptions", "m", {}, files={"file": wav(1.0), "audio": b"x"})
    none = CleanRequest("/v1/audio/transcriptions", "m", {})
    refused = AzureSpeech().validate(two)
    assert refused is not None
    assert refused.param == "file"
    refusal = AzureSpeech().validate(none)
    assert refusal is not None
    assert refusal.param == "file"


def test_a_request_for_another_endpoint_or_a_stream_is_refused() -> None:
    other = CleanRequest("/v1/audio/speech", "m", {"input": "x", "voice": "v"})
    assert AzureSpeech().validate(other) is not None
    streamed = CleanRequest(
        "/v1/audio/transcriptions", "m", {}, stream=True, files={"file": wav(1)}
    )
    assert AzureSpeech().validate(streamed) is not None
    with pytest.raises(Untranslatable):
        AzureSpeech().relay(request())


def test_a_good_request_validates() -> None:
    ok = request(body={"language": "en", "response_format": "verbose_json"})
    assert AzureSpeech().validate(ok) is None
