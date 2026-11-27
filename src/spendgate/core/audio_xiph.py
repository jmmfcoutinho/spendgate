"""Ogg (Opus, Vorbis) and FLAC lengths, counted from the packets and frames (ADR-0029).

The granule of the last Ogg page and FLAC's `total_samples` are numbers the writer chooses; a
decoder ignores them and decodes every packet or frame, and the provider bills what it decoded.
So the count comes from the audio itself and a declared length can only raise it.
"""

# ruff: noqa: PLR2004 - container formats are fixed-offset byte layouts; the numbers are the spec
import re
import struct
from dataclasses import dataclass
from fractions import Fraction

from spendgate.core.audio_codecs import (
    MAX_UNITS,
    OPUS_FRAME_BYTES,
    OPUS_RATE,
    AudioError,
    Counted,
    opus_packet,
    opus_streams,
    vorbis_ident,
)


# --- Ogg ---------------------------------------------------------------------------------------
@dataclass(slots=True)
class _Stream:
    rate: int
    headers: int  # packets before the audio: 2 for Opus, 3 for Vorbis
    per_packet: int  # Vorbis: most samples a packet decodes to; 0 for Opus (read its TOC)
    capacity_unit: int  # Opus: bytes per frame for all streams; Vorbis: bytes per sample
    packets: int = 0
    in_packet: bool = False
    audio: bool = False
    samples: int = 0
    frames: int = 0
    payload: int = 0
    granule: int = -1


def _ogg_stream(packet: bytes) -> _Stream:
    if packet[:8] == b"OpusHead":
        return _Stream(OPUS_RATE, 2, 0, OPUS_FRAME_BYTES * opus_streams(packet))
    if packet[:7] == b"\x01vorbis":
        info = vorbis_ident(packet)
        return _Stream(info.rate, 3, info.per_packet, info.channels * 4)
    raise AudioError("only Opus and Vorbis in Ogg are supported")


def _ogg_page(stream: _Stream, data: bytes, body: int, table: bytes, *, continued: bool) -> None:
    """Count the packets that start on this page; a packet's first byte is its TOC (Opus)."""
    if not continued:
        stream.in_packet = False  # a packet cut short on the last page is lost, not continued
    offset = body
    for lace in table:
        if not stream.in_packet:
            stream.audio = stream.packets >= stream.headers
            stream.packets += 1
            if stream.audio and stream.per_packet:
                stream.samples += stream.per_packet
            elif stream.audio:
                samples, frames = opus_packet(data, offset, lace)
                stream.samples += samples
                stream.frames += frames
        if stream.audio:
            stream.payload += lace
        stream.in_packet = lace == 255
        offset += lace


def _ogg_granule(stream: _Stream, granule: int) -> None:
    if granule < 0:
        return  # no packet ends on this page
    if granule < stream.granule:
        raise AudioError("Ogg granule positions go backwards")
    stream.granule = granule


def ogg(data: bytes) -> Counted:
    pos, units = 0, 0
    streams: dict[int, _Stream] = {}
    while pos + 27 <= len(data):
        if data[pos : pos + 4] != b"OggS":
            raise AudioError("Ogg page expected")
        granule, serial = struct.unpack_from("<qI", data, pos + 6)
        count = data[pos + 26]
        body = pos + 27 + count
        table = data[pos + 27 : body]
        size = sum(table)
        if body > len(data) or body + size > len(data):
            raise AudioError("Ogg page is truncated")
        stream = streams.get(serial)
        if stream is None and size:
            stream = streams[serial] = _ogg_stream(data[body : body + size])
        units += 1 + count  # a page and its lacing values
        if units > MAX_UNITS:
            raise AudioError("the Ogg file has too many packets")
        if stream is not None:
            _ogg_page(stream, data, body, table, continued=bool(data[pos + 5] & 1))
            _ogg_granule(stream, granule)
        pos = body + size
    if pos != len(data):
        raise AudioError("trailing bytes after the last Ogg page")
    if not streams:
        raise AudioError("Ogg without a stream")
    seconds, payload, capacity = Fraction(0), 0, 0
    for stream in streams.values():
        # The granule is the writer's claim; the packets are what decodes. The larger wins.
        samples = max(stream.samples, stream.granule)
        seconds += Fraction(samples, stream.rate)
        payload += stream.payload
        units_held = stream.frames if not stream.per_packet else samples
        capacity += units_held * stream.capacity_unit
    return Counted(seconds, payload, capacity)


# --- FLAC --------------------------------------------------------------------------------------
_FLAC_SYNC = re.compile(rb"\xff[\xf8\xf9]")
# Most sync candidates one FLAC file may have. Each is a header check on the event loop; 25 MB of
# nearly silent audio in 20 ms frames is about 230k frames, and real music far fewer.
MAX_FLAC_CANDIDATES = 300_000
_FLAC_RATES = (0, 88200, 176400, 192000, 8000, 16000, 22050, 24000, 32000, 44100, 48000, 96000)
_FLAC_BITS = (0, 8, 12, 0, 16, 20, 24, 32)  # by sample size code; 0: from STREAMINFO (3 reserved)
_FLAC_BLOCKS = (0, 192, 576, 1152, 2304, 4608, 0, 0, *(256 << n for n in range(8)))
_BLOCK_EXTRA = (0, 0, 0, 0, 0, 0, 1, 2, 0, 0, 0, 0, 0, 0, 0, 0)  # bytes of an explicit size
_RATE_EXTRA = (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 2, 0)  # bytes of an explicit rate
# Length of the UTF-8-like coded frame number by its first byte: leading one bits, 0 if invalid.
_CODED = tuple(
    1 if ones == 0 else ones if 2 <= ones <= 7 else 0
    for ones in (8 - (~first & 0xFF).bit_length() for first in range(256))
)


def _crc8_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        table.append(crc)
    return tuple(table)


_CRC8 = _crc8_table()
_CRC_AFTER_SYNC = {second: _CRC8[_CRC8[0xFF] ^ second] for second in (0xF8, 0xF9)}


@dataclass(frozen=True, slots=True)
class _Info:
    rate: int
    channels: int
    bits: int
    samples: int  # declared total, 0 when unknown


def _flac_info(data: bytes) -> tuple[_Info, int]:
    """STREAMINFO, and where the frames start (after the last metadata block)."""
    if len(data) < 42 or (data[4] & 0x7F) != 0 or int.from_bytes(data[5:8], "big") < 34:
        raise AudioError("FLAC STREAMINFO missing")
    packed = int.from_bytes(data[18:26], "big")  # rate:20 channels:3 bits:5 samples:36
    info = _Info(
        packed >> 44, ((packed >> 41) & 7) + 1, ((packed >> 36) & 31) + 1, packed & ((1 << 36) - 1)
    )
    pos, last = 4, False
    while not last:
        if pos + 4 > len(data):
            raise AudioError("FLAC metadata is truncated")
        last = bool(data[pos] & 0x80)
        pos += 4 + int.from_bytes(data[pos + 1 : pos + 4], "big")
    if pos >= len(data):
        raise AudioError("FLAC has no audio frames")
    return info, pos


def _flac_frame(data: bytes, pos: int, info: _Info) -> tuple[int, int, int] | None:
    """(block size, rate, most bytes the frame can take) of a valid header at `pos`.

    The checks are the ones a decoder makes before it decodes a frame: no reserved code, a
    well-formed frame number and a matching CRC-8. Anything else is not a frame to it either.
    """
    if pos + 6 > len(data):
        return None
    codes, layout = data[pos + 2], data[pos + 3]
    block_code, rate_code = codes >> 4, codes & 15
    if not block_code or rate_code == 15 or layout > 0xAF or layout & 1 or (layout & 0xE) == 6:
        return None
    coded = _CODED[data[pos + 4]]
    end = pos + 4 + coded
    crc_at = end + _BLOCK_EXTRA[block_code] + _RATE_EXTRA[rate_code]
    if not coded or crc_at >= len(data):
        return None
    crc = _CRC_AFTER_SYNC[data[pos + 1]]
    for byte in data[pos + 2 : crc_at]:
        crc = _CRC8[crc ^ byte]
    if crc != data[crc_at] or any((b & 0xC0) != 0x80 for b in data[pos + 5 : end]):
        return None
    sizes = end + _BLOCK_EXTRA[block_code]
    block = _FLAC_BLOCKS[block_code] or int.from_bytes(data[end:sizes], "big") + 1
    if rate_code >= 12:
        rate = int.from_bytes(data[sizes:crc_at], "big") * (1000, 1, 10)[rate_code - 12]
    else:
        rate = _FLAC_RATES[rate_code] or info.rate
    bits = _FLAC_BITS[(layout >> 1) & 7] or info.bits
    channels = (layout >> 4) + 1 if layout < 0x80 else 2
    return (block, rate, _frame_bytes(block, channels, bits)) if rate else None


def _frame_bytes(block: int, channels: int, bits: int) -> int:
    """Most bytes a frame can take: header, verbatim subframes (side channel one bit wider), CRC."""
    return 18 + channels * (block * (bits + 1) // 8 + 7)


def flac(data: bytes) -> Counted:
    info, start = _flac_info(data)
    by_rate: dict[int, int] = {}
    frames: list[tuple[int, int]] = []  # (position, most bytes the frame can take)
    for candidates, match in enumerate(_FLAC_SYNC.finditer(data, start)):
        if candidates >= MAX_FLAC_CANDIDATES:
            raise AudioError("the FLAC file has too many frame candidates")
        header = _flac_frame(data, match.start(), info)
        if header is not None:
            block, rate, most = header
            by_rate[rate] = by_rate.get(rate, 0) + block
            frames.append((match.start(), most))
    if not frames:
        raise AudioError("no FLAC frames found")
    ends = [pos for pos, _ in frames[1:]] + [len(data)]
    covered = sum(min(end - pos, most) for (pos, most), end in zip(frames, ends, strict=True))
    payload = len(data) - frames[0][0]
    # Bytes no frame can hold are audio a stricter decoder might still find: refuse, as MP3 does.
    if payload - covered > max(payload // 50, 8192):
        raise AudioError("too much of the FLAC audio could not be parsed")
    counted = sum((Fraction(samples, rate) for rate, samples in by_rate.items()), Fraction(0))
    declared = Fraction(info.samples, info.rate) if info.samples and info.rate else Fraction(0)
    return Counted(max(counted, declared))
