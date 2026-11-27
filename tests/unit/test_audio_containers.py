"""How each container is counted (ADR-0029): the packet, frame and sample-table paths, and what is
refused because the count would be a guess."""

import struct

import pytest

from spendgate.core.audio import AudioError, measure
from spendgate.core.audio_codecs import opus_packet, opus_streams, vorbis_ident
from spendgate.core.audio_xiph import MAX_FLAC_CANDIDATES
from tests.unit import audio_builders as fx

_OPUS_HEAD = b"OpusHead\x01\x01" + struct.pack("<HIhB", 0, 48000, 0, 0)
_OPUS_TAGS = b"OpusTags" + b"\x00" * 8


# --- Opus and Vorbis packets ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("packet", "expected"),
    [
        (b"\xf8", (960, 1)),  # CELT 20 ms, one frame
        (b"\x00", (480, 1)),  # SILK 10 ms
        (b"\x79", (1920, 2)),  # Hybrid 20 ms, two frames (code 1)
        (b"\xe2", (240, 2)),  # CELT 2.5 ms, two frames of different sizes (code 2)
        (b"\xfb\x03", (2880, 3)),  # CELT 20 ms, code 3: three frames
        (b"\x1b\x03", (5760, 48)),  # SILK 60 ms x 3 is over 120 ms: invalid, held at the most
        (b"\xfb\x00", (5760, 48)),  # code 3 with no frames: invalid
        (b"\xfb", (5760, 48)),  # code 3 without its frame count
        (b"", (5760, 48)),  # an empty packet: a decoder may conceal it
    ],
)
def test_opus_packet_durations_follow_the_toc(packet: bytes, expected: tuple[int, int]) -> None:
    assert opus_packet(packet, 0, len(packet)) == expected


def test_opus_header_stream_count() -> None:
    assert opus_streams(_OPUS_HEAD) == 1
    assert opus_streams(_OPUS_HEAD[:18] + b"\x01\x03\x02" + b"\x00" * 6) == 3
    for bad in (b"OpusHead", _OPUS_HEAD[:18] + b"\x01", _OPUS_HEAD[:18] + b"\x01\x00\x00"):
        with pytest.raises(AudioError):
            opus_streams(bad)


def _vorbis_ident(rate: int = 44100, channels: int = 1, sizes: int = 0xB8) -> bytes:
    return b"\x01vorbis" + struct.pack("<IBI", 0, channels, rate) + b"\x00" * 12 + bytes([sizes, 1])


def test_vorbis_header_must_name_rate_channels_and_block_sizes() -> None:
    assert vorbis_ident(_vorbis_ident()).per_packet == 1024
    for bad in (_vorbis_ident()[:29], _vorbis_ident(channels=0), _vorbis_ident(sizes=0x5B)):
        with pytest.raises(AudioError):
            vorbis_ident(bad)


# --- Ogg -----------------------------------------------------------------------------------------
def _opus_ogg(*audio_pages: bytes) -> bytes:
    return fx._page(7, 0, _OPUS_HEAD) + fx._page(7, 0, _OPUS_TAGS, 1) + b"".join(audio_pages)


def _raw_page(flags: int, granule: int, seq: int, table: bytes, body: bytes) -> bytes:
    head = b"OggS\x00" + bytes([flags]) + struct.pack("<qIII", granule, 7, seq, 0)
    return head + bytes([len(table)]) + table + body


def test_ogg_packet_spanning_pages_is_counted_once() -> None:
    packet = b"\xf8" + b"\x00" * 600  # 601 bytes: laced 255, 255, 91
    first = _raw_page(0, -1, 2, bytes([255]), packet[:255])
    rest = _raw_page(1, 960, 3, bytes([255, 91]), packet[255:])
    assert measure(_opus_ogg(first + rest)).duration_ms == 20


def test_ogg_continued_page_without_a_started_packet_counts_as_a_new_packet() -> None:
    page = _raw_page(1, 960, 2, bytes([3]), b"\xf8\x00\x00")
    assert measure(_opus_ogg(page)).duration_ms == 20


def test_ogg_multiplexed_streams_add_up() -> None:
    opus, vorbis = fx.ogg_opus(1.0, pre_skip=0), fx.ogg_vorbis(1.0)
    assert measure(opus + vorbis).duration_ms == 1000 + measure(vorbis).duration_ms


def test_ogg_granule_above_the_packets_raises_the_measure() -> None:
    page = fx._packets_page(7, 48000 * 3, [b"\xf8"], 2)  # one 20 ms packet, "3 s"
    assert measure(_opus_ogg(page)).duration_ms == 3000


@pytest.mark.parametrize(
    "data",
    [
        fx.ogg_opus(1.0) + b"\x00" * 30,
        fx.ogg_opus(1.0) + b"OggS" + b"\x00" * 22 + b"\x05",
        fx.ogg_opus(1.0)[:-2],
        fx._page(7, 0, b"") * 3,
        _opus_ogg(fx._packets_page(7, 4800, [b"\xf8"], 2), fx._packets_page(7, 960, [b"\xf8"], 3)),
    ],
    ids=["trailing bytes", "short lacing table", "short body", "no stream", "granule backwards"],
)
def test_ogg_that_cannot_be_counted_is_rejected(data: bytes) -> None:
    with pytest.raises(AudioError):
        measure(data)


# --- FLAC ----------------------------------------------------------------------------------------
_INFO = fx.flac(0.0)[:42]  # STREAMINFO at 44.1 kHz, 16-bit, total samples unknown


def _frame(codes: int, layout: int, tail: bytes = b"", number: bytes = b"\x00") -> bytes:
    header = b"\xff\xf8" + bytes([codes, layout]) + number + tail
    return header + bytes([fx._crc8(header)]) + b"\x00" * 6


@pytest.mark.parametrize(
    ("frame", "ms"),
    [
        (_frame(0xC9, 0x08), 93),  # 4096 samples, rate code 9 (44.1 kHz), 16-bit
        (_frame(0x1E, 0x00, b"\x00\x04"), 4800),  # 192 samples, rate code 14: 4 x 10 Hz
        (_frame(0x2D, 0x80, b"\x1f\x40"), 72),  # 576 samples, rate code 13: 8000 Hz; left/side
        (_frame(0x3C, 0x10, b"\x08"), 144),  # 1152 samples, rate code 12: 8 kHz; stereo
        (_frame(0x60, 0x00, b"\x09"), 1),  # 10 samples (explicit size), rate from STREAMINFO
        (_frame(0xC0, 0x00, number=b"\xc2\x80"), 93),  # a two-byte frame number
    ],
)
def test_flac_frame_headers_give_their_block_and_rate(frame: bytes, ms: int) -> None:
    assert measure(_INFO + frame).duration_ms == ms


@pytest.mark.parametrize(
    "frame",
    [
        _frame(0x00, 0x00),  # reserved block size
        _frame(0xCF, 0x00),  # invalid rate
        _frame(0xC0, 0xB0),  # reserved channel assignment
        _frame(0xC0, 0x06),  # reserved sample size
        _frame(0xC0, 0x01),  # reserved bit
        _frame(0xC0, 0x00, number=b"\x80"),  # a continuation byte cannot start a number
        _frame(0xC0, 0x00, number=b"\xc2\x00"),  # a two-byte number without its continuation
        b"\xff\xf8\xc0\x00\x00\x00" + b"\x00" * 6,  # wrong CRC-8
        _frame(0xCD, 0x00, b"\x00\x00"),  # explicit rate of 0 Hz
        b"\xff\xf8\xc0\x00\x00",  # cut short
    ],
)
def test_flac_invalid_frame_headers_are_not_frames(frame: bytes) -> None:
    with pytest.raises(AudioError, match="no FLAC frames"):
        measure(_INFO + frame)


def test_flac_without_a_rate_anywhere_is_rejected() -> None:
    info = bytearray(_INFO)
    info[18:21] = b"\x00\x00\x00"  # STREAMINFO rate 0, and the frame says "from STREAMINFO"
    with pytest.raises(AudioError):
        measure(bytes(info) + _frame(0xC0, 0x00))


def test_flac_declared_length_above_the_frames_raises_the_measure() -> None:
    info = bytearray(_INFO)
    info[21:26] = (44100 * 2).to_bytes(5, "big")  # STREAMINFO: 2 s
    assert measure(bytes(info) + _frame(0xC0, 0x00)).duration_ms == 2000


def test_flac_metadata_that_runs_past_the_file_is_rejected() -> None:
    open_ended = b"fLaC\x00\x00\x00\x22" + _INFO[8:42]  # STREAMINFO not marked last
    for data in (open_ended, open_ended + b"\x84\xff\xff\xff"):
        with pytest.raises(AudioError):
            measure(data)


def test_flac_endless_sync_candidates_are_cut_off() -> None:
    with pytest.raises(AudioError, match="too many frame candidates"):
        measure(_INFO + b"\xff\xf8" * (MAX_FLAC_CANDIDATES + 1))


# --- MP4 -----------------------------------------------------------------------------------------
_FTYP = fx._box(b"ftyp", b"M4A \x00\x00\x00\x00M4A mp42isom")


def _m4a(*traks: bytes, extra: bytes = b"", media: int = 0, timescale: int = 1000) -> bytes:
    moov = fx._box(b"moov", fx.mvhd(timescale, 0) + b"".join(traks) + extra)
    return _FTYP + moov + fx._box(b"mdat", b"\x00" * media)


def _asc(kind: int, rate: int, channels: int = 1, sbr: tuple[int, int] | None = None) -> bytes:
    """An AudioSpecificConfig: object type, rate index (or an explicit rate), channels."""
    rates = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000)

    def rate_bits(value: int) -> str:
        return f"{rates.index(value):04b}" if value in rates else f"1111{value:024b}"

    def kind_bits(value: int) -> str:
        return f"{value:05b}" if value < 31 else f"11111{value - 32:06b}"

    bits = kind_bits(kind) + rate_bits(rate) + f"{channels:04b}"
    if sbr:
        bits += rate_bits(sbr[0]) + kind_bits(sbr[1])
    bits += "0" * (-len(bits) % 8)
    return int(bits, 2).to_bytes(len(bits) // 8, "big")


def _aac(config: bytes = b"", *, oti: int = 0x40, options: bytes = b"\x00") -> bytes:
    return fx.mp4a(child=fx.esds(config or _asc(2, 44100), oti, options))


@pytest.mark.parametrize(
    ("config", "ms"),
    [
        (_asc(2, 44100), 24),  # LC: 1024 samples at 44.1 kHz
        (_asc(5, 24000, sbr=(48000, 2)), 43),  # explicit SBR: the 24 kHz core is what counts
        (_asc(29, 22050, 2, sbr=(44100, 2)), 47),  # parametric stereo, core 22.05 kHz
        (_asc(2, 16000), 64),  # 16 kHz from the rate table
        (_asc(2, 7999), 129),  # an explicit rate (index 15)
        (_asc(2, 44100, channels=7), 24),  # channel configuration 7 is 8 channels
    ],
)
def test_mp4_aac_frames_count_at_the_core_rate(config: bytes, ms: int) -> None:
    data = _m4a(fx.aac_track(1, entry=_aac(config)), media=8)
    assert measure(data).duration_ms == ms


@pytest.mark.parametrize("version", [1, 2])
def test_mp4_quicktime_sound_descriptions(version: int) -> None:
    entry = fx.mp4a(version=version)
    assert measure(_m4a(fx.aac_track(43, entry=entry), media=344)).duration_ms == 999


def test_mp4_quicktime_wave_box_and_es_descriptor_options() -> None:
    wave = fx._box(b"wave", fx._box(b"frma", b"mp4a") + fx.esds())
    assert measure(_m4a(fx.aac_track(43, entry=fx.mp4a(child=wave)), media=344)).duration_ms == 999
    # stream dependence, URL and OCR stream: each adds its fields before the decoder config
    options = b"\xe0" + b"\x00\x01" + b"\x03url" + b"\x00\x02"
    entry = _aac(options=options)
    assert measure(_m4a(fx.aac_track(43, entry=entry), media=344)).duration_ms == 999


def test_mp4_fragments_with_per_sample_sizes_and_header_defaults() -> None:
    trex = fx._full(b"trex", struct.pack(">IIIII", 1, 1, 1024, 0, 0))
    tfhd = fx._full(b"tfhd", struct.pack(">IQII", 1, 0, 1, 7), flags=0x13)
    sized = fx._full(b"trun", struct.pack(">IIIIII", 2, 0, 1024, 9, 1024, 9), flags=0x301)
    plain = fx._full(b"trun", struct.pack(">III", 3, 0, 0), flags=0x005)  # size 7 from tfhd
    moof = fx._box(b"moof", fx._box(b"mfhd", b"\x00" * 8) + fx._box(b"traf", tfhd + sized + plain))
    data = (
        _m4a(fx.aac_track(0), extra=fx._box(b"mvex", trex)) + moof + fx._box(b"mdat", b"\x00" * 39)
    )
    assert measure(data).duration_ms == 117  # 5 frames of 1024 samples at 44.1 kHz


def test_mp4_fragments_take_the_default_size_from_the_track_extends() -> None:
    trex = fx._full(b"trex", struct.pack(">IIIII", 1, 1, 1024, 10, 0))
    tfhd = fx._full(b"tfhd", struct.pack(">I", 1))
    trun = fx._full(b"trun", struct.pack(">I", 43))
    moof = fx._box(b"moof", fx._box(b"traf", tfhd + trun))
    data = (
        _m4a(fx.aac_track(0), extra=fx._box(b"mvex", trex)) + moof + fx._box(b"mdat", b"\x00" * 430)
    )
    assert measure(data).duration_ms == 999


@pytest.mark.parametrize(
    ("traf", "match"),
    [
        (fx._full(b"tfhd", struct.pack(">I", 9)), "undeclared track"),
        (fx._full(b"trun", struct.pack(">I", 1)), "before its fragment header"),
        (
            fx._full(b"tfhd", struct.pack(">I", 1)) + fx._full(b"trun", b"\x00\x00\x00\x09", 0x200),
            "truncated",
        ),
    ],
)
def test_mp4_fragments_that_cannot_be_counted_are_rejected(traf: bytes, match: str) -> None:
    data = _m4a(fx.aac_track(0)) + fx._box(b"moof", fx._box(b"traf", traf))
    with pytest.raises(AudioError, match=match):
        measure(data)


def test_mp4_sample_size_table_is_summed() -> None:
    data = _m4a(fx.aac_track(3, sizes=[5, 6, 7]), media=18)
    assert measure(data).duration_ms == 70


def test_mp4_declared_movie_length_above_the_frames_raises_the_measure() -> None:
    mehd = fx._full(b"mehd", struct.pack(">I", 7000))
    data = _m4a(fx.aac_track(43), extra=fx._box(b"mvex", mehd), media=344)
    assert measure(data).duration_ms == 7000


def _version(entry: bytes, version: int) -> bytes:
    out = bytearray(entry)
    struct.pack_into(">H", out, 16, version)
    return bytes(out)


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (_m4a(fx.aac_track(1, entry=_aac(_asc(42, 48000)))), "not AAC"),  # USAC
        (_m4a(fx.aac_track(1, entry=_aac(_asc(2, 44100, channels=8)))), "not AAC"),  # reserved
        (_m4a(fx.aac_track(1, entry=_aac(_asc(5, 24000, sbr=(48000, 5))))), "not AAC"),
        (_m4a(fx.aac_track(1, entry=_aac(b"\x12"))), "truncated"),
        (_m4a(fx.aac_track(1, entry=_aac(oti=0x6B))), "not AAC"),  # MP3 in MP4
        (_m4a(fx.aac_track(1, entry=fx._box(b"alac", b"\x00" * 28))), "not AAC"),
        (_m4a(fx.aac_track(1, entry=fx.mp4a(child=fx._box(b"free", b"")))), "configuration"),
        (_m4a(fx.aac_track(1, entry=_version(fx.mp4a(), 3))), "version"),
        (_m4a(fx.aac_track(1), fx.aac_track(1), media=16), "same id"),
        (_m4a(fx.aac_track(3, table=fx._full(b"stz2", b"\x00" * 8))), "compact"),
        (_m4a(fx.aac_track(3, table=fx._full(b"stco", b"\x00" * 4))), "sample size table"),
        (_m4a(fx.aac_track(3, table=fx._full(b"stsz", struct.pack(">II", 0, 3)))), "truncated"),
        (_m4a(fx.aac_track(43)) + fx._box(b"mdat", b"\x00" * 9000), "larger than its sample"),
        (_m4a(extra=fx._box(b"mvex", fx._full(b"mehd", b"\x00" * 4)), timescale=0), "movie header"),
        (_m4a(), "without an audio track"),
        (_FTYP + fx._box(b"free", b"") * 100_001, "too many boxes"),
        (_FTYP + fx._box(b"moov", b"\x00\x00\x00\x30mvhd"), "truncated"),
    ],
)
def test_mp4_that_cannot_be_counted_is_rejected(data: bytes, match: str) -> None:
    with pytest.raises(AudioError, match=match):
        measure(data)


def test_mp4_esds_descriptors_must_be_complete() -> None:
    for broken in (
        fx._full(b"esds", b"\x04\x01\x00"),  # not an ES descriptor
        fx._full(b"esds", b"\x03\x7f\x00\x01\x00"),  # longer than its box
    ):
        with pytest.raises(AudioError):
            measure(_m4a(fx.aac_track(1, entry=fx.mp4a(child=broken))))


# --- WAV -----------------------------------------------------------------------------------------
def _riff(fmt: bytes, *chunks: bytes) -> bytes:
    body = b"WAVE" + _chunk(b"fmt ", fmt) + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _chunk(tag: bytes, body: bytes) -> bytes:
    return tag + struct.pack("<I", len(body)) + body + b"\x00" * (len(body) & 1)


def _fmt(tag: int, channels: int, rate: int, bits: int, width: int | None = None) -> bytes:
    align = channels * (width if width is not None else (bits + 7) // 8)
    return struct.pack("<HHIIHH", tag, channels, rate, rate * align, align, bits)


def _extensible(sub: int, bits: int = 16) -> bytes:
    return (
        _fmt(0xFFFE, 1, 8000, bits)
        + struct.pack("<HHI", 22, bits, 4)
        + struct.pack("<H", sub)
        + b"\x00" * 14
    )


@pytest.mark.parametrize(
    ("fmt", "data", "ms"),
    [
        (_fmt(1, 2, 8000, 24), b"\x00" * 48000, 1000),  # 24-bit stereo PCM
        (_fmt(3, 1, 8000, 32), b"\x00" * 32000, 1000),  # IEEE float
        (_fmt(6, 1, 8000, 8), b"\x00" * 8000, 1000),  # A-law
        (_fmt(7, 1, 8000, 8), b"\x00" * 8000, 1000),  # mu-law
        (_extensible(1), b"\x00" * 16000, 1000),  # WAVE_FORMAT_EXTENSIBLE carrying PCM
    ],
)
def test_wav_sample_formats(fmt: bytes, data: bytes, ms: int) -> None:
    assert measure(_riff(fmt, _chunk(b"data", data))).duration_ms == ms


def test_wav_chunks_after_the_data_are_skipped_and_more_data_adds_up() -> None:
    fmt = _fmt(1, 1, 8000, 16)
    second = _riff(
        fmt,
        _chunk(b"data", b"\x00" * 16000),
        _chunk(b"LIST", b"abc"),
        _chunk(b"data", b"\x00" * 16000),
    )
    assert measure(second).duration_ms == 2000
    junk = _riff(fmt, _chunk(b"data", b"\x00" * 16000)) + b"\x01\x02\x03\x04" + b"\x00" * 15996
    assert measure(junk).duration_ms == 2000  # bytes that are not a chunk may be samples


@pytest.mark.parametrize(
    "data",
    [
        _riff(_fmt(0x11, 1, 8000, 4, 1), _chunk(b"data", b"\x00" * 100)),  # IMA ADPCM
        _riff(_fmt(1, 0, 8000, 16), _chunk(b"data", b"\x00" * 100)),  # no channels
        _riff(_fmt(3, 1, 8000, 24), _chunk(b"data", b"\x00" * 100)),  # 24-bit float
        _riff(_fmt(1, 1, 8000, 16)[:12] + b"\x03\x00\x10\x00", _chunk(b"data", b"\x00" * 100)),
        _riff(
            _fmt(1, 1, 8000, 16),
            _chunk(b"fmt ", _fmt(1, 1, 4000, 16)),
            _chunk(b"data", b"\x00" * 100),
        ),
        _riff(_fmt(0xFFFE, 1, 8000, 16) + b"\x00\x00", _chunk(b"data", b"\x00" * 100)),
    ],
    ids=["adpcm", "no channels", "bad float", "align disagrees", "two fmt", "short extensible"],
)
def test_wav_that_cannot_be_counted_is_rejected(data: bytes) -> None:
    with pytest.raises(AudioError):
        measure(data)


# --- WebM / Matroska -----------------------------------------------------------------------------
def _mkv(tracks: bytes, *blocks: bytes, info: bytes = b"") -> bytes:
    header = fx._ebml(b"\x1a\x45\xdf\xa3", fx._ebml(b"\x42\x82", b"webm"))
    cluster = fx._ebml(
        b"\x1f\x43\xb6\x75", fx._ebml(b"\xe7", b"\x00") + b"".join(blocks), unknown=True
    )
    return header + fx._ebml(b"\x18\x53\x80\x67", info + tracks + cluster, unknown=True)


def _block(payload: bytes, track: int = 1, flags: int = 0x80) -> bytes:
    return fx._ebml(b"\xa3", bytes([0x80 | track]) + b"\x00\x00" + bytes([flags]) + payload)


def _xiph(*packets: bytes) -> bytes:
    sizes = b"".join(bytes([255] * (len(p) // 255) + [len(p) % 255]) for p in packets[:-1])
    return bytes([len(packets) - 1]) + sizes + b"".join(packets)


_VORBIS_PRIVATE = _xiph(
    _vorbis_ident() + b"\x00" * 300, b"\x03vorbis" + b"\x00" * 300, b"\x05vorbis"
)


def test_webm_vorbis_blocks_are_held_at_half_the_long_block() -> None:
    tracks = fx.tracks(fx.track_entry(codec=b"A_VORBIS", private=_VORBIS_PRIVATE))
    assert measure(_mkv(tracks, *[_block(b"\x00\x00")] * 43)).duration_ms == 999


def test_webm_laced_blocks_count_each_packet() -> None:
    laced = _block(b"\x02" + b"\x01\x01" + b"\xf8\xf8\xf8", flags=0x82)  # Xiph lacing, 3 packets
    assert measure(_mkv(fx.tracks(), laced)).duration_ms == 360  # 120 ms each at the most
    tracks = fx.tracks(fx.track_entry(codec=b"A_VORBIS", private=_VORBIS_PRIVATE))
    assert measure(_mkv(tracks, *[laced] * 50)).duration_ms == 3483  # 150 x 1024 at 44.1 kHz


def test_webm_other_tracks_are_ignored_and_multistream_opus_is_read() -> None:
    head = _OPUS_HEAD[:18] + b"\x01\x02\x01\x00\x01"
    tracks = fx.tracks(fx.track_entry(1, b"A_OPUS", head), fx.track_entry(2, b"V_VP8"))
    data = _mkv(tracks, *[_block(b"\xf8")] * 10, _block(b"\x00" * 50, track=2))
    assert measure(data).duration_ms == 200  # ten 20 ms packets; the video block adds nothing


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (_mkv(fx.tracks(fx.track_entry(codec=b"A_AAC")), _block(b"\x00")), "only Opus and Vorbis"),
        (
            _mkv(
                fx.tracks(fx.track_entry(codec=b"A_VORBIS", private=b"\x01\x00\x00")),
                _block(b"\x00"),
            ),
            "CodecPrivate",
        ),
        (
            _mkv(
                fx.tracks(fx.track_entry(codec=b"A_VORBIS", private=b"\x02\xff\xff")),
                _block(b"\x00"),
            ),
            "CodecPrivate",
        ),
        (
            _mkv(fx.tracks(fx.track_entry(2, b"V_VP8")), _block(b"\x00", track=2)),
            "without an audio track",
        ),
        (_mkv(fx.tracks(), _block(b"\x00", track=5)), "undeclared track"),
        (_mkv(fx.tracks(fx.track_entry(), fx.track_entry())), "same number"),
        (_mkv(fx.tracks(), fx._ebml(b"\xa3", b"\x81\x00\x00")), "truncated"),
        (_mkv(fx.tracks(), _block(b"", flags=0x82)), "truncated"),
        (_mkv(fx.tracks(fx._ebml(b"\xae", fx._ebml(b"\x86", b"A_OPUS")))), "number or codec"),
        (_mkv(fx.tracks(fx._ebml(b"\xae", b"\xd7\x85\x01"))), "track entry"),
        (
            _mkv(
                fx.tracks(
                    fx._ebml(
                        b"\xae",
                        fx._ebml(b"\xd7", b"\x01")
                        + fx._ebml(b"\x86", b"A_OPUS")
                        + fx._ebml(b"\x6d\x80", b""),
                    )
                )
            ),
            "content encoding",
        ),
    ],
)
def test_webm_that_cannot_be_counted_is_rejected(data: bytes, match: str) -> None:
    with pytest.raises(AudioError, match=match):
        measure(data)
