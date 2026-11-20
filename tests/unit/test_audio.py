"""Audio duration from bytes: exact per container, rounded up, never guessed (ported tests)."""

import random
import struct
import time

import pytest

from spendgate.core.audio import MAX_MATROSKA_ELEMENTS, AudioError, measure
from tests.unit import audio_builders as fx


def test_wav_is_exact_and_rounded_up() -> None:
    assert measure(fx.wav(3.0)).duration_ms == 3000
    assert measure(fx.wav(3.0)).container == "wav"
    assert measure(fx.wav(0.0005, rate=8000)).duration_ms == 1  # 4 frames at 8 kHz = 0.5 ms


def test_wav_streamed_header_uses_what_is_there() -> None:
    data = bytearray(fx.wav(2.0))
    data[40:44] = b"\xff\xff\xff\xff"  # data chunk size unknown (streamed)
    assert measure(bytes(data)).duration_ms == 2000


def test_wav_skips_unknown_chunks_before_data() -> None:
    data = fx.wav(1.0)
    extra = b"LIST" + (3).to_bytes(4, "little") + b"abc" + b"\x00"  # odd size is padded
    data = data[:36] + extra + data[36:]
    assert measure(data).duration_ms == 1000


def test_mp3_frames_with_id3_and_trailer() -> None:
    # 100 frames x 1152 / 44100 = 2.6122 s
    plain = measure(fx.mp3(100))
    assert plain.container == "mp3"
    assert plain.duration_ms == 2613
    assert measure(fx.mp3(100, id3=300)).duration_ms == 2613
    assert measure(fx.mp3(100) + b"TAG" + b"\x00" * 125).duration_ms == 2613


def test_mp3_with_garbage_gap_resynchronises() -> None:
    data = fx.mp3(60) + b"\x00" * 700 + fx.mp3(60)
    assert measure(data).duration_ms == 3135  # 120 frames


def test_mp3_mostly_unparseable_is_rejected() -> None:
    with pytest.raises(AudioError):
        measure(fx.mp3(5) + b"\x00" * 100_000)


def test_ogg_opus_removes_pre_skip() -> None:
    assert measure(fx.ogg_opus(4.0)).duration_ms == 4000
    assert measure(fx.ogg_opus(4.0)).container == "ogg"


def test_ogg_vorbis() -> None:
    assert measure(fx.ogg_vorbis(5.0)).duration_ms == 5000


def test_flac_streaminfo() -> None:
    assert measure(fx.flac(6.0)).duration_ms == 6000
    assert measure(fx.flac(6.0)).container == "flac"


def test_mp4_movie_header_and_moov_first_or_last() -> None:
    assert measure(fx.mp4(12.5)).duration_ms == 12500
    assert measure(fx.mp4(12.5, moov_last=False)).container == "mp4"


def test_mp4_without_moov_is_rejected() -> None:
    data = fx.mp4(3.0)
    with pytest.raises(AudioError):
        measure(data[: data.index(b"moov") - 4])


def test_webm_header_duration_and_block_scan_agree_on_the_larger() -> None:
    declared = measure(fx.webm([0, 20, 40], duration_ticks=9000.0))
    assert declared.container == "webm"
    assert declared.duration_ms == 9000
    # MediaRecorder style: no Duration, unknown-size cluster; the last block plus a 120 ms margin
    scanned = measure(fx.webm([0, 20, 40, 5000], cluster_ms=1000))
    assert scanned.duration_ms == 6120


def test_webm_cannot_hide_length_behind_a_small_declared_duration() -> None:
    assert measure(fx.webm([0, 30_000], duration_ticks=1.0)).duration_ms >= 30_000


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"short",
        b"\x00" * 100,
        b"RIFF\x00\x00\x00\x00WAVE",
        b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00" + b"\x00" * 16,  # no data chunk
        b"OggS" + b"\x00" * 40,
        b"fLaC" + b"\x00" * 60,
        fx.flac(0.0),
        fx.wav(0.0),
        b"\xff\xfb\x90\x00" + b"\x00" * 413,  # one frame only
        b"%PDF-1.7 not audio at all",
    ],
)
def test_unmeasurable_is_rejected(data: bytes) -> None:
    with pytest.raises(AudioError):
        measure(data)


def test_unsupported_ogg_codec_is_rejected() -> None:
    data = bytearray(fx.ogg_opus(1.0))
    data[data.index(b"OpusHead") : data.index(b"OpusHead") + 8] = b"Speex   "
    with pytest.raises(AudioError):
        measure(bytes(data))


FIXTURES = [
    fx.wav(1.0),
    fx.mp3(20),
    fx.ogg_opus(1.0),
    fx.ogg_vorbis(1.0),
    fx.flac(1.0),
    fx.mp4(1.0),
    fx.webm([0, 20, 40], duration_ticks=100.0),
]


@pytest.mark.parametrize("index", range(len(FIXTURES)))
def test_truncated_or_mutated_files_only_ever_raise_audio_error(index: int) -> None:
    """No crash, no hang: every outcome is a measure or an AudioError."""
    rng = random.Random(index)  # noqa: S311 - deterministic mutation, not security
    original = FIXTURES[index]
    candidates = [original[:cut] for cut in range(0, len(original), max(len(original) // 60, 1))]
    for _ in range(150):
        mutated = bytearray(original)
        for _ in range(rng.randint(1, 6)):
            mutated[rng.randrange(len(mutated))] = rng.randrange(256)
        candidates.append(bytes(mutated))
    for data in candidates:
        try:
            result = measure(data)
        except AudioError:
            continue
        assert result.duration_ms > 0


# --- header variants and malformed containers ----------------------------------------------------
def _frames(b1: int, b2: int, length: int, count: int = 10) -> bytes:
    return (bytes([0xFF, b1, b2, 0x00]) + b"\x00" * (length - 4)) * count


@pytest.mark.parametrize(
    ("b1", "b2", "length", "samples", "rate"),
    [
        (0xFF, 0x10, 32, 384, 44100),  # MPEG-1 layer I, 32 kbit/s
        (0xFD, 0x10, 104, 1152, 44100),  # MPEG-1 layer II, 32 kbit/s
        (0xF3, 0x80, 208, 576, 22050),  # MPEG-2 layer III, 64 kbit/s
    ],
)
def test_mp3_layers_and_versions(b1: int, b2: int, length: int, samples: int, rate: int) -> None:
    expected = -(-10 * samples * 1000 // rate)
    assert measure(_frames(b1, b2, length)).duration_ms == expected


@pytest.mark.parametrize(
    ("b1", "b2"),
    [(0xEB, 0x90), (0xF9, 0x90), (0xFB, 0x00), (0xFB, 0xF0), (0xFB, 0x9C)],
    ids=["reserved version", "reserved layer", "free bitrate", "bad bitrate", "bad sample rate"],
)
def test_mp3_invalid_headers_are_not_frames(b1: int, b2: int) -> None:
    with pytest.raises(AudioError):
        measure(_frames(b1, b2, 417))


def test_mp3_partial_header_at_the_end_and_false_sync_are_skipped() -> None:
    assert measure(fx.mp3(100) + b"\xff\xfb").duration_ms == 2613
    false_sync = b"\xff\xfb\x90\x00" + b"\x00" * 500
    assert measure(fx.mp3(60) + b"\x00" * 9 + false_sync + fx.mp3(60)).duration_ms == 3135


def test_mp3_id3_with_bad_size_or_footer() -> None:
    with pytest.raises(AudioError):
        measure(b"ID3\x04\x00\x00\x80\x00\x00\x00" + fx.mp3(20))
    footer = b"ID3\x04\x00\x10\x00\x00\x00\x00" + b"3DI" + b"\x00" * 7
    assert measure(footer + fx.mp3(100)).duration_ms == 2613


def test_wav_without_a_usable_format_is_rejected() -> None:
    fmt_short = b"RIFF\x00\x00\x00\x00WAVEfmt \x04\x00\x00\x00\x00\x00\x00\x00"
    zero_rate = bytearray(fx.wav(1.0))
    zero_rate[28:32] = b"\x00\x00\x00\x00"
    data_first = b"RIFF\x00\x00\x00\x00WAVEdata\x04\x00\x00\x00\x00\x00\x00\x00"
    for data in (fmt_short, bytes(zero_rate), data_first):
        with pytest.raises(AudioError):
            measure(data)


def _box(kind: bytes, body: bytes, *, large: bool = False) -> bytes:
    if large:
        return struct.pack(">I", 1) + kind + struct.pack(">Q", 16 + len(body)) + body
    return struct.pack(">I", 8 + len(body)) + kind + body


_FTYP = _box(b"ftyp", b"M4A \x00\x00\x00\x00M4A mp42isom")


def test_mp4_version_one_headers_and_large_boxes() -> None:
    mvhd = _box(
        b"mvhd", b"\x01\x00\x00\x00" + struct.pack(">QQIQ", 0, 0, 1000, 7500) + b"\x00" * 80
    )
    assert measure(_FTYP + _box(b"moov", mvhd, large=True)).duration_ms == 7500


def test_mp4_fragmented_needs_a_declared_total() -> None:
    mvhd = _box(b"mvhd", b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, 1000, 0) + b"\x00" * 80)
    mehd = _box(b"mehd", b"\x00\x00\x00\x00" + struct.pack(">I", 4000))
    with pytest.raises(AudioError):
        measure(_FTYP + _box(b"moov", mvhd + _box(b"mvex", b"")))
    assert measure(_FTYP + _box(b"moov", mvhd + _box(b"mvex", mehd))).duration_ms == 4000


def test_mp4_unknown_duration_is_not_a_guess() -> None:
    mvhd = _box(b"mvhd", b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, 0, 5) + b"\x00" * 80)
    with pytest.raises(AudioError):
        measure(_FTYP + _box(b"moov", mvhd))


def test_ogg_without_a_sample_rate_or_final_position_is_rejected() -> None:
    with pytest.raises(AudioError):
        measure(fx.ogg_vorbis(1.0, rate=0))
    head = b"OpusHead\x01\x01" + struct.pack("<HIhB", 312, 48000, 0, 0)
    with pytest.raises(AudioError):
        measure(fx._page(7, -1, head))


def _webm_with(info: bytes, cluster_body: bytes) -> bytes:
    header = fx._ebml(b"\x1a\x45\xdf\xa3", fx._ebml(b"\x42\x82", b"webm"))
    cluster = fx._ebml(b"\x1f\x43\xb6\x75", cluster_body, unknown=True)
    return header + fx._ebml(b"\x18\x53\x80\x67", info + cluster, unknown=True)


def test_webm_block_duration_counts() -> None:
    block = fx._ebml(b"\xa1", b"\x81" + struct.pack(">h", 0) + b"\x80" + b"\x00" * 5)
    group = fx._ebml(b"\xa0", block + fx._ebml(b"\x9b", struct.pack(">H", 2000)))
    data = _webm_with(b"", fx._ebml(b"\xe7", struct.pack(">H", 500)) + group)
    assert measure(data).duration_ms == 2500 + 120


def test_webm_zero_timecode_scale_is_rejected() -> None:
    info = fx._ebml(b"\x15\x49\xa9\x66", fx._ebml(b"\x2a\xd7\xb1", b"\x00"))
    with pytest.raises(AudioError):
        measure(_webm_with(info, fx._ebml(b"\xe7", b"\x00")))


def _big_element(ident: bytes, size: int) -> bytes:
    """An element with a 4-byte EBML size (up to 256 MiB) and a body of 0xFF bytes."""
    return ident + (0x10000000 | size).to_bytes(4, "big") + b"\xff" * size


_BLOCK = fx._ebml(b"\xa3", b"\x81\x00\x01\x80" + b"\x00" * 9)  # one valid block at tick 1
_INFO = b"\x15\x49\xa9\x66\x01\xff\xff\xff\xff\xff\xff\xff"  # Info, unknown size


def test_t_aud_01_webm_with_a_huge_timecode_scale_is_rejected_fast() -> None:
    """SR-03: a several-MiB TimecodeScale element made the duration a multi-MiB integer."""
    info = _INFO + _big_element(b"\x2a\xd7\xb1", 4 * 1024 * 1024)
    data = _webm_with(info, fx._ebml(b"\xe7", b"\x00") + _BLOCK)
    start = time.perf_counter()
    with pytest.raises(AudioError):
        measure(data)
    assert time.perf_counter() - start < 0.05


def test_nf_05_a_webm_of_endless_void_elements_is_cut_off_at_the_element_cap() -> None:
    """NF-05: 24 MiB of 2-byte Void elements was 12 M loop turns (about 7 s) on the event loop."""
    void = b"\xec\x80"
    data = _webm_with(b"", void * (MAX_MATROSKA_ELEMENTS + 10))
    with pytest.raises(AudioError, match="too many elements"):
        measure(data)


def test_nf_05_a_real_sized_webm_is_under_the_element_cap() -> None:
    block = fx._ebml(b"\xa3", b"\x81" + struct.pack(">h", 0) + b"\x80" + b"\x00" * 40)
    data = _webm_with(b"", fx._ebml(b"\xe7", b"\x00") + block * 600_000)
    assert measure(data).duration_ms > 0


@pytest.mark.parametrize("ident", [b"\x2a\xd7\xb1", b"\xe7", b"\x9b"])
def test_t_aud_02_ebml_integers_longer_than_eight_bytes_are_rejected(ident: bytes) -> None:
    for size in (9, 16, 1000):
        element = ident + (0x4000 | size).to_bytes(2, "big") + b"\x01" * size
        if ident == b"\x2a\xd7\xb1":
            data = _webm_with(_INFO + element, fx._ebml(b"\xe7", b"\x00") + _BLOCK)
        elif ident == b"\xe7":
            data = _webm_with(b"", element + _BLOCK)
        else:  # BlockDuration inside a BlockGroup
            data = _webm_with(b"", fx._ebml(b"\xe7", b"\x00") + b"\xa0\x80" + element + _BLOCK)
        with pytest.raises(AudioError):
            measure(data)


def test_t_aud_03_a_duration_beyond_24_hours_is_rejected_in_every_container() -> None:
    day = 24 * 3600
    assert measure(fx.wav(5.0)).duration_ms == 5000
    with pytest.raises(AudioError):
        measure(fx.mp4(day + 10, timescale=1))
    with pytest.raises(AudioError):
        measure(fx.flac(day + 10, rate=1000))
    # Matroska: a maximal 8-byte TimecodeScale makes any block a multi-year claim
    info = fx._ebml(b"\x15\x49\xa9\x66", fx._ebml(b"\x2a\xd7\xb1", b"\xff" * 8))
    with pytest.raises(AudioError):
        measure(
            _webm_with(
                info,
                fx._ebml(b"\xe7", b"\x00") + fx._ebml(b"\xa3", b"\x81\x7f\xff\x80" + b"\x00" * 9),
            )
        )
