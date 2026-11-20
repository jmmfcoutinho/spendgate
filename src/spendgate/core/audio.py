"""Audio duration measured from the bytes themselves (stdlib only, no decoding).

Transcription is priced by duration, so spendgate measures it before forwarding a byte and never
trusts what the caller says. Magic bytes decide the container, never a file name: WAV, MP3/MPEG
audio, MP4/M4A, Ogg (Opus, Vorbis), FLAC, WebM/Matroska. Anything else, anything truncated or
inconsistent, and anything whose duration would be a guess raises `AudioError`; the caller refuses
the request (`invalid_request`).

Rounding is UP to the next millisecond, and where a container offers two measures the larger wins,
so the measured duration is never below the real one. Rounding to the catalog's granularity is
done once, in `core.cost`.
"""

# ruff: noqa: PLR2004, PLR0912 - container formats are fixed-offset byte layouts; the numbers are the spec
import math
import struct
from dataclasses import dataclass
from fractions import Fraction


class AudioError(ValueError):
    """The audio cannot be measured with certainty."""


@dataclass(frozen=True, slots=True)
class Measured:
    container: str  # wav | mp3 | mp4 | ogg | flac | webm
    duration_ms: int  # rounded up


# Most elements one Matroska file may have. The parse is synchronous on the event loop, so this
# bounds the stall (a 24 MiB file of empty Void elements took seconds, NF-05) while leaving room
# for a real 24 MiB Opus file (about 600k blocks).
MAX_MATROSKA_ELEMENTS = 1_000_000
MAX_SECONDS = 24 * 3600  # no transcription upload is longer; a bigger claim is junk (SR-03)


def _ms(seconds: Fraction) -> int:
    if seconds <= 0:
        raise AudioError("the audio has no duration")
    if seconds > MAX_SECONDS:
        raise AudioError("the audio claims a duration beyond 24 hours")
    return -((-seconds * 1000).__floor__())  # ceiling


def measure(data: bytes) -> Measured:
    """Return the container and duration of `data`, or raise `AudioError`."""
    if len(data) < 12:
        raise AudioError("file too small to be audio")
    head = data[:12]
    try:
        if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            return Measured("wav", _ms(_wav(data)))
        if head[:4] == b"OggS":
            return Measured("ogg", _ms(_ogg(data)))
        if head[:4] == b"fLaC":
            return Measured("flac", _ms(_flac(data)))
        if head[:4] == b"\x1a\x45\xdf\xa3":
            return Measured("webm", _ms(_matroska(data)))
        if head[4:8] == b"ftyp":
            return Measured("mp4", _ms(_mp4(data)))
        if head[:3] == b"ID3" or head[0] == 0xFF:
            return Measured("mp3", _ms(_mp3(data)))
    except (struct.error, IndexError, ArithmeticError) as exc:
        raise AudioError("the audio file is truncated or malformed") from exc
    raise AudioError("unsupported or unrecognised audio format")


# --- WAV ---------------------------------------------------------------------------------------
def _wav(data: bytes) -> Fraction:
    pos, byte_rate, frame_bytes = 12, 0, 0
    while pos + 8 <= len(data):
        tag = data[pos : pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        if tag == b"fmt ":
            if size < 16 or body + 16 > len(data):
                raise AudioError("WAV fmt chunk is truncated")
            _fmt, _ch, _rate, byte_rate, frame_bytes, _bits = struct.unpack_from(
                "<HHIIHH", data, body
            )
        elif tag == b"data":
            if not byte_rate:
                raise AudioError("WAV data before a usable fmt chunk")
            available = len(data) - body
            # 0 and 0xFFFFFFFF are the markers of a file written as a stream: use what is there.
            length = available if size in (0, 0xFFFFFFFF) else min(size, available)
            if frame_bytes:
                length -= length % frame_bytes
            return Fraction(length, byte_rate)
        pos = body + size + (size & 1)
    raise AudioError("WAV has no data chunk")


# --- MP3 / MPEG audio --------------------------------------------------------------------------
_BITRATES = {
    # (version family, layer) -> kbit/s by index 1..14
    ("1", 1): (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    ("1", 2): (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    ("1", 3): (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    ("2", 1): (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    ("2", 2): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    ("2", 3): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mpeg_header(data: bytes, pos: int) -> tuple[int, int, int] | None:
    """(frame length, samples, sample rate) for a valid MPEG audio header at `pos`, else None."""
    if pos + 4 > len(data):
        return None
    b0, b1, b2 = data[pos], data[pos + 1], data[pos + 2]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version = (b1 >> 3) & 3  # 3 = MPEG1, 2 = MPEG2, 0 = MPEG2.5, 1 = reserved
    layer = 4 - ((b1 >> 1) & 3)  # bits 01 = III, 10 = II, 11 = I
    if version == 1 or layer == 4:
        return None
    bitrate_index, rate_index, padding = (b2 >> 4) & 15, (b2 >> 2) & 3, (b2 >> 1) & 1
    if bitrate_index in (0, 15) or rate_index == 3:
        return None
    family = "1" if version == 3 else "2"
    bitrate = _BITRATES[(family, layer)][bitrate_index - 1] * 1000
    rate = _SAMPLE_RATES[version][rate_index]
    if layer == 1:
        length, samples = (12 * bitrate // rate + padding) * 4, 384
    elif layer == 2 or family == "1":
        length, samples = 144 * bitrate // rate + padding, 1152
    else:
        length, samples = 72 * bitrate // rate + padding, 576
    return (length, samples, rate) if length > 4 else None


def _mp3(data: bytes) -> Fraction:
    pos = 0
    if data[:3] == b"ID3":
        if len(data) < 10 or any(b & 0x80 for b in data[6:10]):
            raise AudioError("malformed ID3 tag")
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        pos = 10 + size + (10 if data[5] & 0x10 else 0)
    end = len(data)
    if end - pos >= 128 and data[end - 128 : end - 125] == b"TAG":
        end -= 128
    total, frames, covered, start = Fraction(0), 0, 0, None
    while pos < end:
        header = _mpeg_header(data, pos)
        if header is None:
            # Not on a frame: resynchronise on the next valid header that is followed by another.
            nxt = _resync(data, pos + 1, end)
            if nxt is None:
                break
            pos = nxt
            continue
        length, samples, rate = header
        if start is None:
            start = pos
        total += Fraction(samples, rate)
        frames += 1
        covered += length
        pos += length
    if frames < 3 or start is None:
        raise AudioError("no MPEG audio frames found")
    payload = end - start
    if payload - covered > max(payload // 50, 8192):
        raise AudioError("too much of the MPEG audio could not be parsed")
    return total


def _resync(data: bytes, pos: int, end: int) -> int | None:
    while True:
        pos = data.find(b"\xff", pos, end)
        if pos < 0:
            return None
        header = _mpeg_header(data, pos)
        if header is not None:
            following = pos + header[0]
            if following >= end or _mpeg_header(data, following) is not None:
                return pos
        pos += 1


# --- MP4 / M4A ---------------------------------------------------------------------------------
def _mp4_boxes(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """[(type, body start, body end)] of the boxes in data[start:end]."""
    boxes: list[tuple[bytes, int, int]] = []
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            if kind in (b"mdat", b"free", b"skip") and size >= header:
                size = end - pos  # truncated media data is not our concern
            else:
                raise AudioError("MP4 box is truncated")
        boxes.append((kind, pos + header, pos + size))
        pos += size
    return boxes


def _mp4_time(data: bytes, body: int) -> Fraction | None:
    """Duration in seconds from an `mvhd` or `mdhd` box body (version 0 or 1)."""
    version = data[body]
    if version == 1:
        timescale, duration = struct.unpack_from(">IQ", data, body + 20)
        unknown = 0xFFFFFFFFFFFFFFFF
    else:
        timescale, duration = struct.unpack_from(">II", data, body + 12)
        unknown = 0xFFFFFFFF
    if timescale == 0 or duration == unknown:
        return None
    return Fraction(duration, timescale)


def _mp4(data: bytes) -> Fraction:
    best = Fraction(0)
    saw_moov = fragmented = False
    for kind, body, end in _mp4_boxes(data, 0, len(data)):
        if kind != b"moov":
            continue
        saw_moov = True
        for sub, sbody, send in _mp4_boxes(data, body, end):
            if sub == b"mvhd":
                value = _mp4_time(data, sbody)
                best = max(best, value or Fraction(0))
            elif sub == b"mvex":
                fragmented = True
                for mkind, mbody, _mend in _mp4_boxes(data, sbody, send):
                    if mkind == b"mehd":
                        version = data[mbody]
                        fmt = ">Q" if version == 1 else ">I"
                        best = max(
                            best,
                            Fraction(struct.unpack_from(fmt, data, mbody + 4)[0])
                            / _mvhd_timescale(data, body, end),
                        )
            elif sub == b"trak":
                for tkind, tbody, tend in _mp4_boxes(data, sbody, send):
                    if tkind != b"mdia":
                        continue
                    for mkind, mbody, _mend in _mp4_boxes(data, tbody, tend):
                        if mkind == b"mdhd":
                            value = _mp4_time(data, mbody)
                            best = max(best, value or Fraction(0))
    if not saw_moov:
        raise AudioError("MP4 has no moov box (not a finished file)")
    if best <= 0 and fragmented:
        raise AudioError("fragmented MP4 without a total duration")
    return best


def _mvhd_timescale(data: bytes, body: int, end: int) -> int:
    for kind, mbody, _mend in _mp4_boxes(data, body, end):
        if kind == b"mvhd":
            version = data[mbody]
            offset = 20 if version == 1 else 12
            timescale = int(struct.unpack_from(">I", data, mbody + offset)[0])
            if timescale:
                return timescale
    raise AudioError("MP4 has no movie header")


# --- Ogg (Opus, Vorbis) ------------------------------------------------------------------------
def _ogg(data: bytes) -> Fraction:
    pos = 0
    streams: dict[int, tuple[Fraction, int]] = {}  # serial -> (seconds per granule, skip)
    last: dict[int, int] = {}
    while pos + 27 <= len(data):
        if data[pos : pos + 4] != b"OggS":
            raise AudioError("Ogg page expected")
        granule, serial = struct.unpack_from("<qI", data, pos + 6)
        count = data[pos + 26]
        table_end = pos + 27 + count
        if table_end > len(data):
            raise AudioError("Ogg page is truncated")
        size = sum(data[pos + 27 : table_end])
        body = table_end
        if body + size > len(data):
            raise AudioError("Ogg page is truncated")
        if serial not in streams and size:
            packet = data[body : body + size]
            if packet[:8] == b"OpusHead" and len(packet) >= 12:
                skip = struct.unpack_from("<H", packet, 10)[0]
                streams[serial] = (Fraction(1, 48000), skip)
            elif packet[:7] == b"\x01vorbis" and len(packet) >= 16:
                rate = struct.unpack_from("<I", packet, 12)[0]
                if not rate:
                    raise AudioError("Vorbis stream without a sample rate")
                streams[serial] = (Fraction(1, rate), 0)
            else:
                raise AudioError("only Opus and Vorbis in Ogg are supported")
        if granule >= 0 and serial in streams:
            last[serial] = granule
        pos = body + size
    if pos != len(data):
        raise AudioError("trailing bytes after the last Ogg page")
    if not streams or not last:
        raise AudioError("Ogg stream without a final position")
    total = Fraction(0)
    for serial, granule in last.items():
        per_granule, skip = streams[serial]
        total += max(granule - skip, 0) * per_granule
    return total


# --- FLAC --------------------------------------------------------------------------------------
def _flac(data: bytes) -> Fraction:
    if len(data) < 42 or (data[4] & 0x7F) != 0 or int.from_bytes(data[5:8], "big") < 34:
        raise AudioError("FLAC STREAMINFO missing")
    packed = int.from_bytes(data[18:26], "big")  # rate:20 channels:3 bits:5 samples:36
    rate, samples = packed >> 44, packed & ((1 << 36) - 1)
    if rate == 0 or samples == 0:
        raise AudioError("FLAC does not declare its length")
    return Fraction(samples, rate)


# --- WebM / Matroska ---------------------------------------------------------------------------
_EBML, _SEGMENT, _INFO, _CLUSTER, _BLOCKGROUP = 0x1A45DFA3, 0x18538067, 0x1549A966, 0x1F43B675, 0xA0
_TIMESCALE, _DURATION, _CL_TIME, _SIMPLEBLOCK, _BLOCK, _BLOCKDUR = (
    0x2AD7B1,
    0x4489,
    0xE7,
    0xA3,
    0xA1,
    0x9B,
)
_DESCEND = frozenset({_SEGMENT, _INFO, _CLUSTER, _BLOCKGROUP})
# Bound on what is added after the last block when a block carries no duration (Opus frames are
# at most 120 ms; the margin makes the measure an upper bound).
_TAIL_MARGIN = Fraction(120, 1000)


def _vint(data: bytes, pos: int, *, keep_marker: bool) -> tuple[int, int, bool]:
    """(value, length, unknown-size) of the EBML variable-length integer at `pos`."""
    first = data[pos]
    length = 1
    mask = 0x80
    while length <= 8 and not first & mask:
        mask >>= 1
        length += 1
    if length > 8:
        raise AudioError("invalid EBML integer")
    if pos + length > len(data):
        raise AudioError("EBML integer is truncated")
    value = first if keep_marker else first & (mask - 1)
    for byte in data[pos + 1 : pos + length]:
        value = (value << 8) | byte
    all_ones = (first & (mask - 1)) == mask - 1 and all(
        b == 0xFF for b in data[pos + 1 : pos + length]
    )
    return value, length, all_ones and not keep_marker


def _uint(data: bytes, body: int, size: int) -> int:
    """An EBML unsigned integer element: 0 to 8 bytes, as the spec says; longer is hostile.

    `int.from_bytes` on a multi-MiB element is quadratic work on the single event loop.
    """
    if size > 8:
        raise AudioError("EBML unsigned integer is longer than 8 bytes")
    return int.from_bytes(data[body : body + size], "big")


def _matroska(data: bytes) -> Fraction:
    pos = 0
    doctype_ok = False
    scale = 1_000_000
    declared = Fraction(0)
    cluster_time = 0
    end_ticks = 0
    saw_block = False
    elements = 0
    while pos < len(data):
        elements += 1
        if elements > MAX_MATROSKA_ELEMENTS:
            raise AudioError("the Matroska file has too many elements")
        ident, id_len, _ = _vint(data, pos, keep_marker=True)
        size, size_len, unknown = _vint(data, pos + id_len, keep_marker=False)
        body = pos + id_len + size_len
        if ident == _EBML:
            doctype_ok = (
                b"webm" in data[body : body + size] or b"matroska" in data[body : body + size]
            )
            pos = body + size
            continue
        if ident in _DESCEND:
            pos = body  # children follow inline; unknown sizes need no special handling
            continue
        if unknown:
            raise AudioError("unknown-size element where a size is required")
        if body + size > len(data):
            # A truncated last element: the blocks before it still count.
            break
        if ident == _TIMESCALE:
            scale = _uint(data, body, size)
        elif ident == _DURATION and size in (4, 8):
            raw = struct.unpack(">f" if size == 4 else ">d", data[body : body + size])[0]
            if math.isfinite(raw) and 0 < raw < 1e15:  # NaN, inf and absurd values are ignored
                declared = Fraction(raw)
        elif ident == _CL_TIME:
            cluster_time = _uint(data, body, size)
        elif ident in (_SIMPLEBLOCK, _BLOCK):
            track, track_len, _ = _vint(data, body, keep_marker=False)
            del track
            relative = struct.unpack_from(">h", data, body + track_len)[0]
            end_ticks = max(end_ticks, cluster_time + relative)
            saw_block = True
        elif ident == _BLOCKDUR:
            end_ticks = max(end_ticks, cluster_time + _uint(data, body, size))
        pos = body + size
    if not doctype_ok:
        raise AudioError("not a WebM or Matroska file")
    return _matroska_seconds(scale, declared, end_ticks, saw_block=saw_block)


def _matroska_seconds(
    scale: int, declared: Fraction, end_ticks: int, *, saw_block: bool
) -> Fraction:
    if scale <= 0:
        raise AudioError("invalid Matroska timecode scale")
    seconds_per_tick = Fraction(scale, 1_000_000_000)
    from_blocks = end_ticks * seconds_per_tick + _TAIL_MARGIN if saw_block else Fraction(0)
    return max(declared * seconds_per_tick, from_blocks)
