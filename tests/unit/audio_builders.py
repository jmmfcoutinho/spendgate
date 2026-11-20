"""Hand-built, tiny audio containers (header-correct, silent payload) for duration tests."""

import io
import struct
import wave


def wav(seconds: float, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def mp3(frames: int, *, id3: int = 0) -> bytes:
    """MPEG-1 Layer III, 128 kbit/s, 44.1 kHz: 417-byte frames of 1152 samples."""
    tag = (
        b"ID3\x04\x00\x00" + bytes([0, 0, (id3 >> 7) & 0x7F, id3 & 0x7F]) + b"\x00" * id3
        if id3
        else b""
    )
    return tag + (b"\xff\xfb\x90\x00" + b"\x00" * 413) * frames


def _page(serial: int, granule: int, payload: bytes, seq: int = 0) -> bytes:
    segments, rest = [], len(payload)
    while rest >= 255:
        segments.append(255)
        rest -= 255
    segments.append(rest)
    head = b"OggS\x00\x00" + struct.pack("<qIII", granule, serial, seq, 0)
    return head + bytes([len(segments)]) + bytes(segments) + payload


def ogg_opus(seconds: float, pre_skip: int = 312) -> bytes:
    opus_head = b"OpusHead\x01\x01" + struct.pack("<HIhB", pre_skip, 48000, 0, 0)
    return _page(7, 0, opus_head) + _page(7, int(seconds * 48000) + pre_skip, b"\x00" * 50, 1)


def ogg_vorbis(seconds: float, rate: int = 44100) -> bytes:
    ident = b"\x01vorbis" + struct.pack("<I", 0) + b"\x01" + struct.pack("<I", rate) + b"\x00" * 12
    return _page(9, 0, ident) + _page(9, int(seconds * rate), b"\x00" * 40, 1)


def flac(seconds: float, rate: int = 44100) -> bytes:
    packed = (rate << 44) | (0 << 41) | (15 << 36) | int(seconds * rate)
    info = b"\x10\x00\x10\x00" + b"\x00" * 6 + packed.to_bytes(8, "big") + b"\x00" * 16
    return b"fLaC" + b"\x80\x00\x00\x22" + info + b"\x00" * 100


def _box(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + kind + body


def mp4(seconds: float, timescale: int = 1000, *, moov_last: bool = True) -> bytes:
    mvhd = _box(
        b"mvhd",
        b"\x00\x00\x00\x00"
        + struct.pack(">IIII", 0, 0, timescale, int(seconds * timescale))
        + b"\x00" * 80,
    )
    mdhd = _box(
        b"mdhd",
        b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, 44100, int(seconds * 44100)) + b"\x00" * 4,
    )
    moov = _box(b"moov", mvhd + _box(b"trak", _box(b"mdia", mdhd)))
    ftyp = _box(b"ftyp", b"M4A \x00\x00\x00\x00M4A mp42isom")
    mdat = _box(b"mdat", b"\x00" * 200)
    return ftyp + mdat + moov if moov_last else ftyp + moov + mdat


def _ebml(ident: bytes, body: bytes, *, unknown: bool = False) -> bytes:
    if unknown:
        return ident + b"\x01\xff\xff\xff\xff\xff\xff\xff" + body
    assert len(body) < 127
    return ident + bytes([0x80 | len(body)]) + body


def webm(
    blocks_ms: list[int], *, duration_ticks: float | None = None, cluster_ms: int = 0
) -> bytes:
    """EBML + Segment(unknown size) + optional Info/Duration + one unknown-size Cluster."""
    header = _ebml(b"\x1a\x45\xdf\xa3", _ebml(b"\x42\x82", b"webm"))
    info = b""
    if duration_ticks is not None:
        info = _ebml(
            b"\x15\x49\xa9\x66",
            _ebml(b"\x2a\xd7\xb1", struct.pack(">I", 1_000_000))
            + _ebml(b"\x44\x89", struct.pack(">d", duration_ticks)),
        )
    blocks = b"".join(
        _ebml(b"\xa3", b"\x81" + struct.pack(">h", ms) + b"\x80" + b"\x00" * 20) for ms in blocks_ms
    )
    cluster = _ebml(
        b"\x1f\x43\xb6\x75", _ebml(b"\xe7", struct.pack(">H", cluster_ms)) + blocks, unknown=True
    )
    return header + _ebml(b"\x18\x53\x80\x67", info + cluster, unknown=True)
