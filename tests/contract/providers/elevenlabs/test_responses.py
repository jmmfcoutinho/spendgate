"""Response side: audio bytes, billed characters, costs, errors, the fake."""

from decimal import ROUND_CEILING, Decimal

import httpx
import pytest

from spendgate.core.cost import Usage, actual_cost
from spendgate.core.errors import ErrorCode
from spendgate.core.outcomes import Outcome
from spendgate.providers.elevenlabs import ElevenLabs
from spendgate.providers.port import CleanRequest, UpstreamResponse
from tests.contract.providers.elevenlabs.helpers import (
    MODEL,
    MP3,
    NOW_MS,
    TEXT,
    VOICE,
    audio,
    catalog,
    deployment,
    error,
    request,
)
from tests.fakes import FakeUpstream, FakeUsage, Script

pytestmark = pytest.mark.anyio
FREE = {429}


# --- the answer: audio bytes, not base64 JSON -----------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "content_type"),
    [("mp3", "audio/mpeg"), ("opus", "audio/opus"), ("wav", "audio/wav"), ("pcm", "audio/pcm")],
)
def test_the_answer_is_raw_audio_with_the_requested_formats_content_type(
    fmt: str, content_type: str
) -> None:
    upstream = UpstreamResponse(200, {"Content-Type": "application/octet-stream"}, MP3)
    out = ElevenLabs().response(request({"response_format": fmt}), upstream)
    assert (out.status, out.body) == (200, MP3)  # the bytes, not base64 and not JSON
    assert out.headers == {"Content-Type": content_type}


def test_the_default_content_type_is_mpeg() -> None:
    assert ElevenLabs().response(request(), audio()).headers["Content-Type"] == "audio/mpeg"


def test_a_success_without_audio_becomes_a_bodiless_502() -> None:
    out = ElevenLabs().response(request(), UpstreamResponse(200, {}, b""))
    assert (out.status, out.body) == (502, b"")


def test_an_error_answer_is_left_for_error_mapping() -> None:
    resp = error("error_500.json", 500)
    assert ElevenLabs().response(request(), resp) is resp


# --- usage: billed characters ---------------------------------------------------------------------


def test_usage_is_the_inputs_character_count() -> None:
    assert ElevenLabs().usage(request(), audio()) == Usage(characters=len(TEXT))


def test_characters_not_bytes_are_counted() -> None:
    assert ElevenLabs().usage(request({"input": "héllo wörld"}), audio()) == Usage(characters=11)


def test_the_character_cost_header_can_only_raise_the_charge() -> None:
    low = ElevenLabs().usage(request(), audio({"character-cost": "10"}))
    high = ElevenLabs().usage(request(), audio({"character-cost": "100"}))
    assert low == Usage(characters=len(TEXT))
    assert high == Usage(characters=100)


def test_the_header_is_found_whatever_its_case_and_junk_is_ignored() -> None:
    assert ElevenLabs().usage(request(), audio({"Character-Cost": "200"})) == Usage(characters=200)
    for junk in ("abc", "-5", "1.5", "", " "):
        assert ElevenLabs().usage(request(), audio({"character-cost": junk})) == Usage(
            characters=len(TEXT)
        )


def test_a_huge_header_is_booked_in_full_and_never_clamped() -> None:
    """The earlier guard capped the charge at twice the reservation; here absurd usage is booked
    in full and flagged by the pipeline (T-PROV), so the adapter reports what it was told."""
    usage = ElevenLabs().usage(request(), audio({"character-cost": "10000000"}))
    assert usage == Usage(characters=10_000_000)


def test_no_usage_on_an_error_answer() -> None:
    assert ElevenLabs().usage(request(), audio(status=500)) is None
    assert ElevenLabs().usage(request(), audio(status=429)) is None


def test_no_usage_when_the_request_has_no_text() -> None:
    assert ElevenLabs().usage(CleanRequest("/v1/audio/speech", "m", {}), audio()) is None


# --- costs: the earlier guard's nano values-----------------------------------------------------

FX = Decimal("0.92")  # the earlier guard booked EUR; spendgate books USD


def reference_nano(characters: int, per_1k: Decimal) -> int:
    """The earlier guard's `eleven_cost_nano`: characters x rate / 1000, one ceiling, in EUR."""
    usd = Decimal(characters) * per_1k / 1000
    return int((usd * FX * 10**9).to_integral_value(rounding=ROUND_CEILING))


def nano_usd(model: str, usage: Usage) -> int:
    entry = catalog().lookup("elevenlabs", model, NOW_MS)
    return int(actual_cost(entry, usage).to_decimal() * 10**9)


def test_the_catalog_still_carries_the_earlier_guards_published_prices() -> None:
    """test_prices_are_the_published_ones: v4 0.08, flash 0.04 USD per 1,000 characters."""
    assert nano_usd("eleven_v4", Usage(characters=1000)) == 80_000_000
    assert nano_usd("eleven_flash_v2_5", Usage(characters=1000)) == 40_000_000
    # 1000 characters of flash: USD 0.04 x 0.92 = EUR 0.0368, the earlier guard's own assertion.
    assert reference_nano(1000, Decimal("0.04")) == 36_800_000


@pytest.mark.parametrize(
    ("model", "per_1k"),
    [
        ("eleven_v4", "0.08"),
        ("eleven_v4_turbo", "0.04"),
        ("eleven_multilingual_v2", "0.08"),
        ("eleven_flash_v2_5", "0.04"),
    ],
)
@pytest.mark.parametrize(
    "header", [None, "10", "100"], ids=["no-header", "header-below", "header-above"]
)
def test_cost_equals_the_earlier_guards_for_every_model_and_header_case(
    model: str, per_1k: str, header: str | None
) -> None:
    """Earlier cases: speaks and charges the characters; header 10 and 100 vs 54 characters."""
    headers = {"character-cost": header} if header else None
    usage = ElevenLabs().usage(request(), audio(headers))
    assert usage is not None
    billed = max(len(TEXT), int(header or 0))
    assert usage == Usage(characters=billed)
    usd = nano_usd(model, usage)
    assert usd == int(Decimal(billed) * Decimal(per_1k) / 1000 * 10**9)
    # The earlier guard converted to EUR once at the end: within one nano of FX x our USD value.
    assert abs(int(Decimal(usd) * FX) - reference_nano(billed, Decimal(per_1k))) <= 1


# --- errors ---------------------------------------------------------------------------------------

ERRORS = [
    ("error_429_concurrency.json", 429, ErrorCode.UPSTREAM_THROTTLED, Outcome.THROTTLED),
    # Out of credits is ours to fix, whatever the status (protocol rule 1).
    ("error_401_quota.json", 401, ErrorCode.UPSTREAM_UNAVAILABLE, Outcome.CLIENT_ERROR),
    ("error_401_quota.json", 429, ErrorCode.UPSTREAM_UNAVAILABLE, Outcome.THROTTLED),
    ("error_401_quota.json", 402, ErrorCode.UPSTREAM_UNAVAILABLE, Outcome.CLIENT_ERROR),
    ("error_401_quota.json", 403, ErrorCode.UPSTREAM_UNAVAILABLE, Outcome.CLIENT_ERROR),
    ("error_404_voice.json", 404, ErrorCode.INVALID_REQUEST, Outcome.CLIENT_ERROR),
    ("error_422.json", 422, ErrorCode.INVALID_REQUEST, Outcome.CLIENT_ERROR),
    ("error_422.json", 400, ErrorCode.INVALID_REQUEST, Outcome.CLIENT_ERROR),
    ("error_422.json", 413, ErrorCode.REQUEST_TOO_LARGE, Outcome.CLIENT_ERROR),
    ("error_500.json", 500, ErrorCode.UPSTREAM_UNAVAILABLE, Outcome.UPSTREAM_5XX),
    ("error_500.json", 503, ErrorCode.UPSTREAM_UNAVAILABLE, Outcome.UPSTREAM_5XX),
]


@pytest.mark.parametrize(("name", "status", "code", "outcome"), ERRORS)
def test_errors_map_to_core_codes_and_outcomes(
    name: str, status: int, code: ErrorCode, outcome: Outcome
) -> None:
    err = ElevenLabs().error(error(name, status))
    assert (err.code, err.status, err.sent) == (code, status, True)
    assert err.outcome(FREE) is outcome
    assert err.not_processed(FREE) is (status == 429)  # only 429 is free by default


def test_retry_after_is_carried_on_a_429_only() -> None:
    err = ElevenLabs().error(error("error_429_concurrency.json", 429, {"Retry-After": "4"}))
    assert err.retry_after_seconds == 4
    junk = ElevenLabs().error(error("error_429_concurrency.json", 429, {"retry-after": "soon"}))
    assert junk.retry_after_seconds is None
    assert (
        ElevenLabs().error(error("error_500.json", 500, {"Retry-After": "4"})).retry_after_seconds
        is None
    )


@pytest.mark.parametrize("raw", [b"", b"<html>", b"[]", b'{"detail": "text"}', b'{"detail": 5}'])
def test_an_unreadable_error_body_still_maps(raw: bytes) -> None:
    err = ElevenLabs().error(UpstreamResponse(503, {}, raw))
    assert (err.code, err.message) == (ErrorCode.UPSTREAM_UNAVAILABLE, "")


def test_the_error_never_echoes_request_text_and_is_kept_short() -> None:
    body = b'{"detail": {"status": "bad", "message": "' + b"x" * 5000 + b'"}}'
    err = ElevenLabs().error(UpstreamResponse(400, {}, body))
    assert len(err.message) <= 200
    assert TEXT not in repr(err)
    assert "xxx" not in repr(err)  # provider text stays out of reprs


# --- against the fake -----------------------------------------------------------------------------


async def send(fake: FakeUpstream, req: CleanRequest) -> httpx.Response:
    sent = ElevenLabs().translate(req, deployment())
    async with fake.client() as client:
        return await client.request(
            sent.method, sent.url, headers=dict(sent.headers), content=sent.body
        )


async def test_the_fake_round_trip_returns_audio_billed_by_the_reported_characters() -> None:
    fake = FakeUpstream(Script(usage=FakeUsage(characters=100)))
    req = request()
    http = await send(fake, req)
    seen = fake.requests[0]
    assert seen.path == f"/v1/text-to-speech/{VOICE}"
    assert seen.query == {"output_format": "mp3_44100_128"}
    assert seen.headers["xi-api-key"] == "fake-xi-key-0000"
    assert seen.json()["model_id"] == MODEL
    upstream = UpstreamResponse(http.status_code, dict(http.headers), http.content)
    assert ElevenLabs().usage(req, upstream) == Usage(characters=100)
    out = ElevenLabs().response(req, upstream)
    assert (out.headers["Content-Type"], out.body) == ("audio/mpeg", http.content)


async def test_the_fake_reporting_fewer_characters_cannot_lower_the_bill() -> None:
    fake = FakeUpstream(Script(usage=FakeUsage(characters=3)))
    req = request()
    http = await send(fake, req)
    upstream = UpstreamResponse(http.status_code, dict(http.headers), http.content)
    assert ElevenLabs().usage(req, upstream) == Usage(characters=len(TEXT))


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (429, ErrorCode.UPSTREAM_THROTTLED),
        (500, ErrorCode.UPSTREAM_UNAVAILABLE),
        (400, ErrorCode.INVALID_REQUEST),
    ],
)
async def test_the_fake_errors_map(status: int, code: ErrorCode) -> None:
    fake = FakeUpstream(Script(status=status, retry_after="3"))
    http = await send(fake, request())
    err = ElevenLabs().error(UpstreamResponse(http.status_code, dict(http.headers), http.content))
    assert err.code is code
