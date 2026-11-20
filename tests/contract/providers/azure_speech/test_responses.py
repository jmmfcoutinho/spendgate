"""Response side: OpenAI shapes, measured-versus-reported usage, costs, errors, the fake."""

import json
from decimal import ROUND_CEILING, Decimal

import httpx
import pytest

from spendgate.core.catalog import Catalog
from spendgate.core.cost import Estimate, Usage, actual_cost, worst_case
from spendgate.core.errors import ErrorCode
from spendgate.core.outcomes import Outcome
from spendgate.providers.azure_speech import AzureSpeech
from spendgate.providers.port import CleanRequest, Json, UpstreamResponse
from tests.contract.providers.azure_speech.helpers import (
    NOW_MS,
    REFERENCE_PER_MINUTE,
    answer,
    catalog,
    deployment,
    reply,
    request,
)
from tests.fakes import FakeUpstream, FakeUsage, Script
from tests.unit.audio_builders import mp3, wav

pytestmark = pytest.mark.anyio
FREE = {429}


def body_of(resp: UpstreamResponse) -> dict[str, Json]:
    loaded: dict[str, Json] = json.loads(resp.body)
    return loaded


# --- the answer in the OpenAI shape ---------------------------------------------------------------


def test_json_answer_is_the_openai_transcription_shape() -> None:
    out = AzureSpeech().response(request(), answer("transcribe_ok.json"))
    assert (out.status, out.headers["Content-Type"]) == (200, "application/json")
    assert body_of(out) == {"text": "Good afternoon. Welcome."}


def test_text_format_is_plain_text() -> None:
    req = request(body={"response_format": "text"})
    out = AzureSpeech().response(req, answer("transcribe_ok.json"))
    assert out.body == b"Good afternoon. Welcome."
    assert out.headers["Content-Type"].startswith("text/plain")


def test_verbose_json_carries_duration_segments_and_language() -> None:
    req = request(body={"response_format": "verbose_json"})
    data = body_of(AzureSpeech().response(req, answer("transcribe_ok.json")))
    assert data["task"] == "transcribe"
    assert data["duration"] == 3.0
    assert data["language"] == "en-US"
    assert data["segments"] == [
        {"id": 0, "start": 0.96, "end": 1.6, "text": "Good afternoon."},
        {"id": 1, "start": 1.7, "end": 2.5, "text": "Welcome."},
    ]
    assert "words" not in data  # only when asked


def test_word_timestamps_only_when_requested() -> None:
    body: dict[str, Json] = {
        "response_format": "verbose_json",
        "timestamp_granularities[]": ["word"],
    }
    data = body_of(AzureSpeech().response(request(body=body), answer("transcribe_ok.json")))
    assert data["words"] == [
        {"word": "Good", "start": 0.96, "end": 1.2},
        {"word": "afternoon.", "start": 1.2, "end": 1.6},
    ]


@pytest.mark.parametrize(
    "raw", [b"", b"not json", b"[]", b'{"phrases": []}', b'{"combinedPhrases": 3}']
)
def test_an_unreadable_success_becomes_a_bodiless_502(raw: bytes) -> None:
    out = AzureSpeech().response(request(), UpstreamResponse(200, {}, raw))
    assert (out.status, out.body) == (502, b"")


def test_an_error_answer_is_left_for_error_mapping() -> None:
    resp = answer("error_400.json", 400)
    assert AzureSpeech().response(request(), resp) is resp


# --- usage: measured, compared with reported, the larger wins -------------------------------------


def test_usage_is_the_measured_duration_when_azure_reports_less() -> None:
    usage = AzureSpeech().usage(request(3.0), reply(1500))
    assert usage == Usage(audio_seconds=Decimal(3))  # never under-bill


def test_usage_is_the_reported_duration_when_azure_reports_more() -> None:
    assert AzureSpeech().usage(request(3.0), reply(4200)) == Usage(audio_seconds=Decimal("4.2"))


def test_usage_agrees_when_both_agree() -> None:
    assert AzureSpeech().usage(request(3.0), reply(3000)) == Usage(audio_seconds=Decimal(3))


def test_a_caller_declared_duration_is_ignored() -> None:
    declared = CleanRequest(
        "/v1/audio/transcriptions",
        "m",
        {"duration": 1, "audio_seconds": 1},
        headers={"x-audio-seconds": "1", "content-duration": "1"},
        files={"file": wav(3.0)},
    )
    assert AzureSpeech().usage(declared, reply(1000)) == Usage(audio_seconds=Decimal(3))


def test_measurement_rounds_up_so_a_fraction_is_never_lost() -> None:
    usage = AzureSpeech().usage(request(audio=mp3(100)), reply(0))
    assert usage is not None
    assert usage.audio_seconds >= Decimal("2.612")  # 100 frames x 1152 / 44100 s = 2.6122 s


@pytest.mark.parametrize(
    "resp",
    [
        reply(None),  # no duration: the full hold stands, as in the earlier guard
        UpstreamResponse(200, {}, b"not json"),
        reply(-5),
        reply(10**12),
        UpstreamResponse(200, {}, b'{"durationMilliseconds": true}'),
        UpstreamResponse(200, {}, b'{"durationMilliseconds": "3000"}'),
        UpstreamResponse(500, {}, b'{"durationMilliseconds": 3000}'),
    ],
)
def test_an_absent_or_implausible_duration_leaves_the_hold_charged(resp: UpstreamResponse) -> None:
    assert AzureSpeech().usage(request(), resp) is None


def test_audio_that_cannot_be_measured_gives_no_usage() -> None:
    assert AzureSpeech().usage(request(audio=b"not audio"), reply(3000)) is None


# --- costs: the earlier guard's nano values, then the bundled catalog---------------------------

FX = Decimal("0.92")  # the earlier guard booked EUR; spendgate books USD, so convert for the parity


def reference_nano(seconds: Decimal, per_minute: Decimal) -> int:
    """The earlier guard's `audio_seconds_cost_nano`: exact seconds, one ceiling to nano, in EUR."""
    usd = seconds / 60 * per_minute
    return int((usd * FX * 10**9).to_integral_value(rounding=ROUND_CEILING))


def nano_usd(cat: Catalog, model: str, usage: Usage) -> int:
    entry = cat.lookup("azure_speech", model, NOW_MS)
    return int(actual_cost(entry, usage).to_decimal() * 10**9)


@pytest.mark.parametrize(
    ("model", "seconds"),
    [
        ("azure-speech", Decimal(3)),
        ("mai-transcribe-2", Decimal(60)),
        ("azure-speech", Decimal(61)),
    ],
)
def test_cost_equals_the_earlier_guards_at_its_rate(model: str, seconds: Decimal) -> None:
    """Earlier cases: 3 s reply (test_speech.py happy path), 60 s MAI ledger row, 61 s file."""
    cat = catalog(REFERENCE_PER_MINUTE)
    usage = AzureSpeech().usage(request(float(seconds)), reply(int(seconds * 1000)))
    assert usage == Usage(audio_seconds=seconds)
    assert usage is not None
    usd = nano_usd(cat, model, usage)
    # Same arithmetic, converted to EUR once at the end: one ceiling each, so within 1 nano.
    assert abs(int(Decimal(usd) * FX) - reference_nano(seconds, REFERENCE_PER_MINUTE)) <= 1
    assert usd == int(seconds / 60 * REFERENCE_PER_MINUTE * 10**9)


def test_a_missing_duration_hold_equals_the_earlier_guards_reservation() -> None:
    """Earlier guard: 3000 ms audio reserves billed_seconds(3000) x the per-minute rate."""
    entry = catalog(REFERENCE_PER_MINUTE).lookup("azure_speech", "azure-speech", NOW_MS)
    hold = worst_case(entry, Estimate(audio_seconds=Decimal(3)))
    assert int(hold.to_decimal() * 10**9) == int(Decimal(3) / 60 * REFERENCE_PER_MINUTE * 10**9)


def test_bundled_catalog_rates_are_the_new_azure_prices() -> None:
    """Rates moved since the earlier guard: azure-speech 0.0072 to 0.0075, MAI to 0.009 USD/min."""
    cat = catalog()
    plain = nano_usd(cat, "azure-speech", Usage(audio_seconds=Decimal(60)))
    mai = nano_usd(cat, "mai-transcribe-2", Usage(audio_seconds=Decimal(60)))
    assert (plain, mai) == (7_500_000, 9_000_000)
    three = nano_usd(cat, "azure-speech", Usage(audio_seconds=Decimal(3)))
    assert three == 375_000  # 3 s at 0.0075/min


def test_the_core_rounds_billed_seconds_up_to_the_catalog_granularity() -> None:
    """Drift from the earlier guard, which charged 1.5 s exactly: the new core bills 2 s."""
    cat = catalog(REFERENCE_PER_MINUTE)
    assert nano_usd(cat, "azure-speech", Usage(audio_seconds=Decimal("1.5"))) == int(
        Decimal(2) / 60 * REFERENCE_PER_MINUTE * 10**9
    )


# --- errors ---------------------------------------------------------------------------------------

ERRORS = [
    ("error_429_throttled.json", 429, ErrorCode.UPSTREAM_THROTTLED, Outcome.THROTTLED),
    # No Speech quota on our resource: ours to fix, never "throttled" (protocol rule 1).
    ("error_429_resource_exhausted.json", 429, ErrorCode.UPSTREAM_UNAVAILABLE, Outcome.THROTTLED),
    ("error_400.json", 400, ErrorCode.INVALID_REQUEST, Outcome.CLIENT_ERROR),
    # Our resource lacks the model: our configuration, not the caller's request.
    ("error_400_enhanced_mode.json", 400, ErrorCode.UPSTREAM_UNAVAILABLE, Outcome.CLIENT_ERROR),
    ("error_401.json", 401, ErrorCode.UPSTREAM_UNAVAILABLE, Outcome.CLIENT_ERROR),
    ("error_401.json", 403, ErrorCode.UPSTREAM_UNAVAILABLE, Outcome.CLIENT_ERROR),
    ("error_400.json", 413, ErrorCode.REQUEST_TOO_LARGE, Outcome.CLIENT_ERROR),
    ("error_400.json", 500, ErrorCode.UPSTREAM_UNAVAILABLE, Outcome.UPSTREAM_5XX),
    ("error_400.json", 503, ErrorCode.UPSTREAM_UNAVAILABLE, Outcome.UPSTREAM_5XX),
]


@pytest.mark.parametrize(("name", "status", "code", "outcome"), ERRORS)
def test_errors_map_to_core_codes_and_outcomes(
    name: str, status: int, code: ErrorCode, outcome: Outcome
) -> None:
    err = AzureSpeech().error(answer(name, status))
    assert (err.code, err.status, err.sent) == (code, status, True)
    assert err.outcome(FREE) is outcome
    assert err.not_processed(FREE) is (status == 429)  # only 429 is free by default


def test_retry_after_is_carried_on_a_429_only() -> None:
    err = AzureSpeech().error(answer("error_429_throttled.json", 429, {"Retry-After": "7"}))
    assert err.retry_after_seconds == 7
    junk = AzureSpeech().error(
        answer("error_429_throttled.json", 429, {"retry-after": "Wed, 1 Jan"})
    )
    assert junk.retry_after_seconds is None
    assert (
        AzureSpeech().error(answer("error_400.json", 500, {"Retry-After": "7"})).retry_after_seconds
        is None
    )


def test_an_unreadable_error_body_still_maps() -> None:
    err = AzureSpeech().error(UpstreamResponse(502, {}, b"<html>bad gateway</html>"))
    assert (err.code, err.message) == (ErrorCode.UPSTREAM_UNAVAILABLE, "")


def test_provider_text_is_kept_short_and_out_of_the_repr() -> None:
    long = UpstreamResponse(400, {}, json.dumps({"message": "x" * 5000}).encode())
    err = AzureSpeech().error(long)
    assert len(err.message) <= 200
    assert "xxx" not in repr(err)


# --- against the fake -----------------------------------------------------------------------------


async def send(
    fake: FakeUpstream, req: CleanRequest, model: str = "azure-speech"
) -> httpx.Response:
    sent = AzureSpeech().translate(req, deployment(model))
    async with fake.client() as client:
        return await client.request(
            sent.method, sent.url, headers=dict(sent.headers), content=sent.body
        )


async def test_the_fake_round_trip_reads_the_measured_duration_and_bills_it() -> None:
    fake = FakeUpstream(Script(usage=FakeUsage(audio_seconds=Decimal(3))))
    req = request(3.0)
    http = await send(fake, req)
    assert http.status_code == 200
    seen = fake.requests[0]
    assert seen.path == "/speechtotext/transcriptions:transcribe"
    assert seen.query == {"api-version": "2025-10-15"}
    assert seen.headers["ocp-apim-subscription-key"] == "fake-speech-key-0000"
    upstream = UpstreamResponse(http.status_code, dict(http.headers), http.content)
    assert AzureSpeech().usage(req, upstream) == Usage(audio_seconds=Decimal(3))
    assert body_of(AzureSpeech().response(req, upstream)) == {"text": "hello"}


async def test_the_fake_reporting_a_short_duration_cannot_lower_the_bill() -> None:
    fake = FakeUpstream(Script(usage=FakeUsage(audio_seconds=Decimal(1))))
    req = request(3.0)
    http = await send(fake, req)
    usage = AzureSpeech().usage(req, UpstreamResponse(200, {}, http.content))
    assert usage == Usage(audio_seconds=Decimal(3))


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
    err = AzureSpeech().error(UpstreamResponse(http.status_code, dict(http.headers), http.content))
    assert err.code is code
