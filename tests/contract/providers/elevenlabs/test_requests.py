"""Request side: validation, the voice id, format mapping, translation, auth, secrecy."""

import json
import logging

import pytest

from spendgate.providers.elevenlabs import ElevenLabs
from spendgate.providers.port import AuthMode, CleanRequest, Deployment, Json, Untranslatable
from tests.contract.providers.elevenlabs.helpers import KEY, MODEL, TEXT, VOICE, deployment, request
from tests.fakes import BASE_URL


def sent_body(body: bytes) -> dict[str, Json]:
    loaded: dict[str, Json] = json.loads(body)
    return loaded


def test_translate_builds_the_text_to_speech_request() -> None:
    out = ElevenLabs().translate(request(), deployment())
    assert out.method == "POST"
    assert out.url == f"{BASE_URL}/v1/text-to-speech/{VOICE}?output_format=mp3_44100_128"
    assert out.headers["xi-api-key"] == KEY
    assert "Authorization" not in out.headers  # ElevenLabs reads xi-api-key only
    assert out.headers["Content-Type"] == "application/json"
    assert sent_body(out.body) == {"model_id": MODEL, "text": TEXT}
    assert not out.stream


def test_the_deployment_model_replaces_the_gateway_model_name() -> None:
    out = ElevenLabs().translate(request(), deployment("eleven_v4"))
    assert sent_body(out.body)["model_id"] == "eleven_v4"


def test_a_base_url_ending_in_v1_is_not_doubled() -> None:
    dep = Deployment("elevenlabs", BASE_URL + "/v1/", MODEL, AuthMode.API_KEY, credential=KEY)
    assert ElevenLabs().translate(request(), dep).url.startswith(f"{BASE_URL}/v1/text-to-speech/")


def test_speed_goes_into_voice_settings_and_nothing_else_is_forwarded() -> None:
    body: dict[str, Json] = {"speed": 1.1, "stream_format": "audio", "model": "ignored"}
    out = ElevenLabs().translate(request(body), deployment())
    assert sent_body(out.body) == {
        "model_id": MODEL,
        "text": TEXT,
        "voice_settings": {"speed": 1.1},
    }


@pytest.mark.parametrize(
    ("fmt", "output", "content_type"),
    [
        ("mp3", "mp3_44100_128", "audio/mpeg"),
        ("opus", "opus_48000_64", "audio/opus"),
        ("wav", "wav_24000", "audio/wav"),
        ("pcm", "pcm_24000", "audio/pcm"),
    ],
)
def test_response_format_maps_to_an_allowlisted_output_format(
    fmt: str, output: str, content_type: str
) -> None:
    out = ElevenLabs().translate(request({"response_format": fmt}), deployment())
    assert out.url.endswith(f"?output_format={output}")
    assert out.headers["Accept"] == content_type


def test_mp3_is_the_default_format() -> None:
    assert ElevenLabs().translate(request(), deployment()).url.endswith("=mp3_44100_128")


@pytest.mark.parametrize("fmt", ["aac", "flac"])
def test_formats_elevenlabs_cannot_make_are_refused(fmt: str) -> None:
    refusal = ElevenLabs().validate(request({"response_format": fmt}))
    assert refusal is not None
    assert refusal.param == "response_format"


@pytest.mark.parametrize("voice", [VOICE, "A" * 10, "a1" * 20, "21m00Tcm4TlvDq8ikWAM"])
def test_valid_voice_ids_pass(voice: str) -> None:
    assert ElevenLabs().validate(request({"voice": voice})) is None


@pytest.mark.parametrize(
    "voice",
    [
        "alloy",  # an OpenAI voice name is not an ElevenLabs id
        "short123",
        "x" * 41,
        "../../v1/user/xxxx",
        "abcdEFGH12/34567890xy",
        "abcdEFGH12?34567890xy",
        "abcdEFGH12 34567890xy",
        "abcdEFGH12-34567890xy",
        "abcdEFGH12\n34567890xy",
        "",
        "voïcevoicevoice1234",
    ],
)
def test_anything_but_an_elevenlabs_voice_id_is_refused(voice: str) -> None:
    refusal = ElevenLabs().validate(request({"voice": voice}))
    assert refusal is not None
    assert refusal.param == "voice"
    with pytest.raises(Untranslatable):
        ElevenLabs().translate(request({"voice": voice}), deployment())


@pytest.mark.parametrize(
    ("body", "param"),
    [
        ({"instructions": "cheerful"}, "instructions"),
        ({"speed": 2.0}, "speed"),
        ({"speed": 0.5}, "speed"),
        ({"speed": "fast"}, "speed"),
        ({"speed": True}, "speed"),
        ({"input": ""}, "input"),
        ({"input": "   "}, "input"),
        ({"input": "x" * 4097}, "input"),
        ({"input": 5}, "input"),
        ({"stream_format": "sse"}, "stream_format"),
        ({"response_format": "mp4"}, "response_format"),
        ({"pronunciation_dictionary_locators": []}, "pronunciation_dictionary_locators"),
        ({"enable_logging": False}, "enable_logging"),
    ],
)
def test_what_elevenlabs_cannot_carry_is_refused_not_dropped(
    body: dict[str, Json], param: str
) -> None:
    refusal = ElevenLabs().validate(request(body))
    assert refusal is not None
    assert refusal.param == param


def test_the_largest_input_is_accepted() -> None:
    assert ElevenLabs().validate(request({"input": "é" * 4096})) is None


def test_a_request_for_another_endpoint_or_a_stream_is_refused() -> None:
    chat = CleanRequest("/v1/chat/completions", "m", {})
    assert ElevenLabs().validate(chat) is not None
    streamed = CleanRequest("/v1/audio/speech", "m", {"input": "x", "voice": VOICE}, stream=True)
    assert ElevenLabs().validate(streamed) is not None
    with pytest.raises(Untranslatable):
        ElevenLabs().relay(request())


def test_a_deployment_without_a_key_fails_closed() -> None:
    bare = Deployment("elevenlabs", BASE_URL, MODEL, AuthMode.API_KEY)
    with pytest.raises(ValueError, match="credential"):
        ElevenLabs().translate(request(), bare)


def test_secrets_stay_out_of_reprs_urls_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    out = ElevenLabs().translate(request(), deployment())
    assert KEY not in out.url
    assert KEY not in repr(out)
    assert KEY not in repr(deployment())
    assert KEY not in caplog.text
    assert TEXT not in repr(out)  # user content stays out of reprs too
