"""Hand-built, tiny audio containers for duration tests: real packet structure, silent payload.

Each builder writes the packets a decoder would decode (MP3 frames, Opus and Vorbis packets, FLAC
frames, AAC samples in the MP4 tables, Matroska blocks), so the measure counts them; the
header fields say the same length (the M-16 tests make them lie, ADR-0029).
"""

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


# --- Ogg -----------------------------------------------------------------------------------------
OPUS_20MS = b"\xf8\x00\x00"  # TOC 0xF8: CELT fullband, one 20 ms frame


def _lacing(size: int) -> list[int]:
    return [255] * (size // 255) + [size % 255]


def _packets_page(serial: int, granule: int, packets: list[bytes], seq: int = 0) -> bytes:
    segments = [lace for packet in packets for lace in _lacing(len(packet))]
    head = b"OggS\x00\x00" + struct.pack("<qIII", granule, serial, seq, 0)
    return head + bytes([len(segments)]) + bytes(segments) + b"".join(packets)


def _page(serial: int, granule: int, payload: bytes, seq: int = 0) -> bytes:
    return _packets_page(serial, granule, [payload], seq)


def _ogg(serial: int, headers: list[bytes], packets: int, samples: int, total: int) -> bytes:
    """Header pages, then `packets` audio packets of `samples` each, 50 to a page."""
    out = [_page(serial, 0, header, seq) for seq, header in enumerate(headers)]
    done = 0
    audio = OPUS_20MS if headers[0].startswith(b"OpusHead") else b"\x00\x00"
    while done < packets:
        count = min(50, packets - done)
        done += count
        out.append(_packets_page(serial, min(done * samples, total), [audio] * count, len(out)))
    return b"".join(out)


def ogg_opus(seconds: float, pre_skip: int = 312) -> bytes:
    """20 ms packets covering `seconds` after the pre-skip; the last granule trims the end."""
    opus_head = b"OpusHead\x01\x01" + struct.pack("<HIhB", pre_skip, 48000, 0, 0)
    tags = b"OpusTags" + struct.pack("<II", 0, 0)
    total = int(seconds * 48000) + pre_skip
    return _ogg(7, [opus_head, tags], -(-total // 960), 960, total)


def ogg_vorbis(seconds: float, rate: int = 44100) -> bytes:
    """Block sizes 256 and 2048: each audio packet decodes to at most 1024 samples."""
    ident = (
        b"\x01vorbis"
        + struct.pack("<IBI", 0, 1, rate)
        + b"\x00" * 12
        + bytes([0xB8, 0x01])  # block sizes 2^8 and 2^11, framing bit
    )
    total = int(seconds * rate)
    headers = [ident, b"\x03vorbis" + b"\x00" * 9, b"\x05vorbis" + b"\x00" * 20]
    return _ogg(9, headers, -(-total // 1024), 1024, total)


# --- FLAC ----------------------------------------------------------------------------------------
def _crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _utf8(number: int) -> bytes:
    if number < 0x80:
        return bytes([number])
    if number < 0x800:
        return bytes([0xC0 | number >> 6, 0x80 | number & 0x3F])
    return bytes([0xE0 | number >> 12, 0x80 | (number >> 6) & 0x3F, 0x80 | number & 0x3F])


def flac_frame(number: int, block: int) -> bytes:
    """A mono frame of `block` silent samples (one constant subframe), rate from STREAMINFO."""
    code = 12 if block == 4096 else 7
    header = b"\xff\xf8" + bytes([code << 4, 0x00]) + _utf8(number)
    if code == 7:
        header += (block - 1).to_bytes(2, "big")
    header += bytes([_crc8(header)])
    return header + b"\x00\x00\x00" + b"\x00\x00"  # constant subframe, CRC-16 (unchecked)


def flac(seconds: float, rate: int = 44100) -> bytes:
    total = int(seconds * rate)
    packed = (rate << 44) | (0 << 41) | (15 << 36) | total
    info = b"\x10\x00\x10\x00" + b"\x00" * 6 + packed.to_bytes(8, "big") + b"\x00" * 16
    frames, number = [], 0
    while number * 4096 < total:
        frames.append(flac_frame(number, min(4096, total - number * 4096)))
        number += 1
    return b"fLaC" + b"\x80\x00\x00\x22" + info + b"".join(frames)


# --- MP4 -----------------------------------------------------------------------------------------
def _box(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + kind + body


def _full(kind: bytes, body: bytes, flags: int = 0) -> bytes:
    return _box(kind, b"\x00" + flags.to_bytes(3, "big") + body)


def _descriptor(tag: int, body: bytes) -> bytes:
    return bytes([tag, len(body)]) + body


def esds(config: bytes = b"\x12\x08", oti: int = 0x40, options: bytes = b"\x00") -> bytes:
    """An ES descriptor carrying an AudioSpecificConfig (default: AAC LC, 44.1 kHz, mono).

    `options` is the ES flags byte and the optional fields it announces.
    """
    decoder = _descriptor(0x04, bytes([oti, 0x15]) + b"\x00" * 11 + _descriptor(0x05, config))
    return _full(b"esds", _descriptor(0x03, b"\x00\x01" + options + decoder + b"\x06\x01\x02"))


def mp4a(rate: int = 44100, child: bytes = b"", *, version: int = 0) -> bytes:
    """An AAC sample entry (`child` defaults to the default `esds`); QuickTime versions 1 and 2
    carry 16 or 36 more bytes before the child boxes."""
    head = b"\x00" * 6 + b"\x00\x01" + struct.pack(">H", version) + b"\x00" * 6
    sound = struct.pack(">HHHHI", 1, 16, 0, 0, rate << 16)
    extra = b"\x00" * {1: 16, 2: 36}.get(version, 0)
    return _box(b"mp4a", head + sound + extra + (child or esds()))


def aac_track(
    samples: int,
    *,
    track_id: int = 1,
    sizes: int | list[int] = 8,
    entry: bytes = b"",
    table: bytes = b"",
) -> bytes:
    """A sound `trak` of `samples` AAC frames (`sizes`: one size for all, or each one's)."""
    stsd = _full(b"stsd", struct.pack(">I", 1) + (entry or mp4a()))
    stts = _full(b"stts", struct.pack(">III", 1, samples, 1024))
    if isinstance(sizes, list):
        stsz = _full(
            b"stsz", struct.pack(">II", 0, samples) + struct.pack(f">{len(sizes)}I", *sizes)
        )
    else:
        stsz = _full(b"stsz", struct.pack(">II", sizes, samples))
    stbl = _box(b"stbl", stsd + stts + (table or stsz))
    mdhd = _full(b"mdhd", struct.pack(">IIII", 0, 0, 44100, samples * 1024) + b"\x00" * 4)
    hdlr = _full(b"hdlr", b"\x00" * 4 + b"soun" + b"\x00" * 13)
    tkhd = _full(b"tkhd", struct.pack(">IIII", 0, 0, track_id, 0) + b"\x00" * 64)
    return _box(b"trak", tkhd + _box(b"mdia", mdhd + hdlr + _box(b"minf", stbl)))


def mvhd(timescale: int, duration: int) -> bytes:
    return _full(b"mvhd", struct.pack(">IIII", 0, 0, timescale, duration) + b"\x00" * 80)


def mp4(seconds: float, timescale: int = 1000, *, moov_last: bool = True) -> bytes:
    """AAC LC at 44.1 kHz: enough 1024-sample frames to cover `seconds`, 8 bytes each."""
    samples = -(-int(seconds * 44100) // 1024)
    moov = _box(b"moov", mvhd(timescale, int(seconds * timescale)) + aac_track(samples))
    ftyp = _box(b"ftyp", b"M4A \x00\x00\x00\x00M4A mp42isom")
    mdat = _box(b"mdat", b"\x00" * (8 * samples))
    return ftyp + mdat + moov if moov_last else ftyp + moov + mdat


# --- WebM ----------------------------------------------------------------------------------------
def _ebml(ident: bytes, body: bytes, *, unknown: bool = False) -> bytes:
    if unknown:
        return ident + b"\x01\xff\xff\xff\xff\xff\xff\xff" + body
    if len(body) < 127:
        return ident + bytes([0x80 | len(body)]) + body
    assert len(body) < 0x3FFF
    return ident + (0x4000 | len(body)).to_bytes(2, "big") + body


def track_entry(number: int = 1, codec: bytes = b"A_OPUS", private: bytes = b"") -> bytes:
    entry = _ebml(b"\xd7", bytes([number])) + _ebml(b"\x86", codec)
    if private:
        entry += _ebml(b"\x63\xa2", private)
    return _ebml(b"\xae", entry)


def tracks(*entries: bytes) -> bytes:
    """A Tracks element; by default track 1 in Opus."""
    return _ebml(b"\x16\x54\xae\x6b", b"".join(entries) or track_entry())


def webm(
    blocks_ms: list[int], *, duration_ticks: float | None = None, cluster_ms: int = 0
) -> bytes:
    """EBML + Segment(unknown size) + optional Info/Duration + Tracks + one unknown-size Cluster.

    Each block holds one 10 ms Opus packet (TOC 0x00) at the given time.
    """
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
    return header + _ebml(b"\x18\x53\x80\x67", info + tracks() + cluster, unknown=True)
