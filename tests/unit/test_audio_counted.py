"""M-16 and T-AUD-04: a header that understates the audio never lowers the hold (ADR-0029).

Each container gets a generated file and a forged twin whose header claims about one second, made
the way the SG-PT-02 forger made its files: only header fields change, so a decoder still decodes
(and a provider still bills) the whole length. The twin is measured at least as long as the real
file, or refused.
"""

import struct
from decimal import Decimal

import pytest

from spendgate.core.audio import AudioError, measure
from spendgate.core.catalog import PriceEntry
from spendgate.core.cost import Estimate
from spendgate.core.estimate import RequestKind, estimate
from tests.unit import audio_builders as fx


# --- the forger (header fields only; the audio is untouched) ------------------------------------
def _ogg_pages(data: bytes) -> list[int]:
    pos, pages = 0, []
    while pos + 27 <= len(data):
        pages.append(pos)
        count = data[pos + 26]
        pos += 27 + count + sum(data[pos + 27 : pos + 27 + count])
    return pages


def forge_ogg_last_granule(data: bytes, granule: int) -> bytes:
    out = bytearray(data)
    struct.pack_into("<q", out, _ogg_pages(data)[-1] + 6, granule)
    return bytes(out)


def forge_ogg_every_granule(data: bytes, divisor: int) -> bytes:
    """Every page's granule divided: still increasing, so nothing goes backwards."""
    out = bytearray(data)
    for page in _ogg_pages(data):
        granule = struct.unpack_from("<q", out, page + 6)[0]
        struct.pack_into("<q", out, page + 6, granule // divisor)
    return bytes(out)


def forge_flac_total_samples(data: bytes, samples: int) -> bytes:
    out = bytearray(data)
    packed = int.from_bytes(out[18:26], "big")
    out[18:26] = ((packed & ~((1 << 36) - 1)) | samples).to_bytes(8, "big")
    return bytes(out)


def forge_mp4_durations(data: bytes, seconds: int, *, timescale: int | None = None) -> bytes:
    """`mvhd` and `mdhd` say `seconds`; optionally `mdhd` also gets another timescale."""
    out = bytearray(data)
    for kind in (b"mvhd", b"mdhd"):
        body = data.index(kind) + 4
        scale = struct.unpack_from(">I", out, body + 12)[0]
        if timescale and kind == b"mdhd":
            scale = timescale
        struct.pack_into(">II", out, body + 12, scale, scale * seconds)
    return bytes(out)


def forge_wav_byte_rate(data: bytes, factor: int) -> bytes:
    out = bytearray(data)
    struct.pack_into("<I", out, 28, struct.unpack_from("<I", out, 28)[0] * factor)
    return bytes(out)


def forge_webm(data: bytes, *, zero_blocks: bool) -> bytes:
    """The cluster timecode zeroed (as the forger did), optionally every block's too."""
    out = bytearray(data)
    pos = data.index(b"\x1f\x43\xb6\x75") + 12  # inside the unknown-size cluster
    out[pos + 2 : pos + 4] = b"\x00\x00"  # Timecode element body
    if zero_blocks:
        pos = 0
        while (pos := data.find(b"\xa3\x96\x81", pos)) >= 0:
            out[pos + 3 : pos + 5] = b"\x00\x00"
            pos += 3
    return bytes(out)


def _webm_30s(*, duration: float) -> bytes:
    """3000 blocks of one 10 ms Opus packet each: 30 s of audio."""
    header = fx._ebml(b"\x1a\x45\xdf\xa3", fx._ebml(b"\x42\x82", b"webm"))
    info = fx._ebml(
        b"\x15\x49\xa9\x66",
        fx._ebml(b"\x2a\xd7\xb1", struct.pack(">I", 1_000_000))
        + fx._ebml(b"\x44\x89", struct.pack(">d", duration)),
    )
    blocks = b"".join(
        fx._ebml(b"\xa3", b"\x81" + struct.pack(">h", ms) + b"\x80" + b"\x00" * 18)
        for ms in range(0, 30_000, 10)
    )
    cluster = fx._ebml(b"\x1f\x43\xb6\x75", fx._ebml(b"\xe7", b"\x00\x00") + blocks, unknown=True)
    return header + fx._ebml(b"\x18\x53\x80\x67", info + fx.tracks() + cluster, unknown=True)


def _held_at_least_the_real_length_or_refused(real: bytes, forged: bytes) -> None:
    truth = measure(real).duration_ms
    try:
        held = measure(forged).duration_ms
    except AudioError:
        return
    assert held >= truth, f"forged file held at {held} ms, real length {truth} ms"


# --- one per container ---------------------------------------------------------------------------
def test_m16_t_aud_04_flac_total_samples_that_understate_never_lower_the_hold() -> None:
    real = fx.flac(30.0, rate=8000)
    forged = forge_flac_total_samples(real, 8000)  # "1 s"
    assert measure(real).duration_ms == 30_000
    assert measure(forged).duration_ms == 30_000  # the frames are counted


def test_m16_t_aud_04_mp4_durations_that_understate_never_lower_the_hold() -> None:
    real = fx.mp4(30.0)
    for forged in (
        forge_mp4_durations(real, 1),
        forge_mp4_durations(real, 1, timescale=90_000_000),  # a timescale chosen to shrink stts
    ):
        assert measure(forged).duration_ms == measure(real).duration_ms >= 30_000


def test_m16_t_aud_04_mp4_sample_tables_that_hide_media_are_refused() -> None:
    real = fx.mp4(30.0)
    count_at = real.index(b"stsz") + 12
    forged = bytearray(real)
    struct.pack_into(">I", forged, count_at, 43)  # one second of frames; mdat still holds 30
    with pytest.raises(AudioError, match="larger than its sample tables"):
        measure(bytes(forged))


def test_m16_t_aud_04_ogg_opus_granules_that_understate_never_lower_the_hold() -> None:
    real = fx.ogg_opus(30.0, pre_skip=0)
    assert measure(real).duration_ms == 30_000
    with pytest.raises(AudioError, match="backwards"):  # the SG-PT-02 forgery: last page only
        measure(forge_ogg_last_granule(real, 48000))
    assert measure(forge_ogg_every_granule(real, 1800)).duration_ms == 30_000  # packets counted


def test_m16_t_aud_04_ogg_vorbis_granules_that_understate_never_lower_the_hold() -> None:
    real = fx.ogg_vorbis(30.0)
    _held_at_least_the_real_length_or_refused(real, forge_ogg_last_granule(real, 44100))
    forged = forge_ogg_every_granule(real, 30)
    assert measure(forged).duration_ms >= 30_000


def test_m16_t_aud_04_wav_byte_rate_that_understates_is_refused() -> None:
    real = fx.wav(30.0)
    assert measure(real).duration_ms == 30_000
    with pytest.raises(AudioError, match="disagree"):
        measure(forge_wav_byte_rate(real, 1000))


def test_m16_t_aud_04_wav_data_size_that_understates_never_lowers_the_hold() -> None:
    real = fx.wav(30.0)
    forged = bytearray(real)
    struct.pack_into("<I", forged, 40, 32_000)  # data chunk "1 s"; the samples follow it
    assert measure(bytes(forged)).duration_ms == 30_000


def test_m16_t_aud_04_webm_timecodes_that_understate_never_lower_the_hold() -> None:
    real = _webm_30s(duration=30_000.0)
    assert measure(real).duration_ms >= 30_000
    for zero_blocks in (False, True):
        forged = forge_webm(_webm_30s(duration=1.0), zero_blocks=zero_blocks)  # Duration: 1 ms
        assert measure(forged).duration_ms >= 30_000  # every Opus packet is counted


def test_m16_t_aud_04_mp3_frame_count_in_a_vbr_header_is_not_read() -> None:
    xing = b"\xff\xfb\x90\x00" + b"\x00" * 32 + b"Xing" + struct.pack(">II", 3, 1) + b"\x00" * 369
    forged = xing + fx.mp3(400)  # the Xing header says one frame
    assert measure(forged).duration_ms >= measure(fx.mp3(400)).duration_ms


# --- the size floor ------------------------------------------------------------------------------
def _opus_ogg_with_packets(packet: bytes, count: int) -> bytes:
    head = b"OpusHead\x01\x01" + struct.pack("<HIhB", 0, 48000, 0, 0)
    pages = [fx._page(7, 0, head), fx._page(7, 0, b"OpusTags" + b"\x00" * 8, 1)]
    pages += [fx._packets_page(7, (i + 1) * 960, [packet], i + 2) for i in range(count)]
    return b"".join(pages)


def test_m16_t_aud_04_the_size_floor_refuses_more_audio_than_the_length_can_carry() -> None:
    # A 20 ms Opus frame holds at most 1275 bytes; 4000 bytes in each is not what a TOC says.
    assert measure(_opus_ogg_with_packets(b"\xf8" + b"\x00" * 1200, 100)).duration_ms == 2000
    with pytest.raises(AudioError, match="more data than its length"):
        measure(_opus_ogg_with_packets(b"\xf8" + b"\x00" * 4000, 100))
    # An AAC frame holds at most 768 bytes per channel.
    samples = 431
    moov = fx._box(b"moov", fx.mvhd(1000, 0) + fx.aac_track(samples, sizes=4000))
    ftyp = fx._box(b"ftyp", b"M4A \x00\x00\x00\x00M4A mp42isom")
    with pytest.raises(AudioError, match="more data than its length"):
        measure(ftyp + fx._box(b"mdat", b"\x00" * 4000 * samples) + moov)


def test_m16_t_aud_04_flac_bytes_no_frame_can_hold_are_refused() -> None:
    real = fx.flac(3.0)
    with pytest.raises(AudioError, match="could not be parsed"):
        measure(real + b"\x00" * 200_000)


# --- the hold itself -----------------------------------------------------------------------------
WHISPER = PriceEntry.model_validate(
    {
        "id": "openai/whisper-1",
        "mode": "transcription",
        "currency": "USD",
        "effective_from": "2026-01-01",
        "source_url": "https://example.com/p",
        "verified": False,
        "meters": {"audio_input_per_minute": "0.006"},
    }
)


@pytest.mark.parametrize(
    "forged",
    [
        forge_flac_total_samples(fx.flac(30.0, rate=8000), 8000),
        forge_mp4_durations(fx.mp4(30.0), 1),
        forge_ogg_every_granule(fx.ogg_opus(30.0, pre_skip=0), 1800),
        forge_webm(_webm_30s(duration=1.0), zero_blocks=True),
    ],
    ids=["flac", "mp4", "ogg", "webm"],
)
def test_m16_a_forged_header_does_not_lower_the_transcription_hold(forged: bytes) -> None:
    outcome = estimate(RequestKind.TRANSCRIPTION, {"file": forged}, entry=WHISPER)
    assert isinstance(outcome, Estimate)
    assert outcome.audio_seconds >= Decimal(30)
