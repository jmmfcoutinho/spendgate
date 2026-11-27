"""MP4 / M4A length, counted from the AAC frames in the sample tables (ADR-0029).

`mvhd`, `mdhd` and `stts` durations are the writer's numbers; a demuxer hands the decoder every
sample in `stsz` (and in each fragment's `trun`), and each AAC sample decodes to 1024 samples at
the core rate of its AudioSpecificConfig. That count is the length; a declared duration can only
raise it. Sound tracks in another codec are refused, and so is media data the tables do not
describe.
"""

# ruff: noqa: PLR2004 - container formats are fixed-offset byte layouts; the numbers are the spec
import struct
import sys
from array import array
from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction

from spendgate.core.audio_codecs import AudioError, Counted

_MAX_BOXES = 100_000  # per level: a fragment a second for a day is about 86k top-level boxes
_WORD = "I" if array("I").itemsize == 4 else "L"
_AAC_OTI = frozenset({0x40, 0x66, 0x67, 0x68})  # MPEG-4 audio, MPEG-2 AAC Main, LC, SSR
# Object types whose access unit is at most 1024 samples at the core rate (USAC is not: refused).
_AAC_TYPES = frozenset({1, 2, 3, 4, 6, 17, 19, 20, 22, 23, 39})
_AAC_RATES = (
    96000,
    88200,
    64000,
    48000,
    44100,
    32000,
    24000,
    22050,
    16000,
    12000,
    11025,
    8000,
    7350,
)
# channelConfiguration -> channels; 0 (a program config element) is taken as 8.
_AAC_CHANNELS = {0: 8, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 8, 11: 7, 12: 8, 13: 24, 14: 8}
AAC_FRAME = 1024
AAC_FRAME_BYTES = 768  # per channel: 6144 bits, the most one AAC frame may take


@dataclass(slots=True)
class _Track:
    handler: bytes = b""
    rate: int = 0  # the lowest AAC core rate of its sample descriptions, 0 if none
    channels: int = 0
    other_codec: bool = False
    has_sizes: bool = False  # an `stsz` was seen (fragmented files carry an empty one)
    samples: int = 0
    sample_bytes: int = 0
    default_size: int = 0  # from `trex`, for fragments


def _boxes(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """(type, body start, body end) of the boxes in data[start:end]."""
    pos, count = start, 0
    while pos + 8 <= end:
        count += 1
        if count > _MAX_BOXES:
            raise AudioError("the MP4 file has too many boxes")
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            if kind in (b"mdat", b"free", b"skip") and size >= header:
                size = end - pos  # truncated media data: the tables still say what decodes
            else:
                raise AudioError("MP4 box is truncated")
        yield kind, pos + header, pos + size
        pos += size


def _mp4_time(data: bytes, body: int) -> Fraction:
    """Duration in seconds from an `mvhd` or `mdhd` box body (version 0 or 1), 0 if unknown."""
    if data[body] == 1:
        timescale, duration = struct.unpack_from(">IQ", data, body + 20)
        unknown = 0xFFFFFFFFFFFFFFFF
    else:
        timescale, duration = struct.unpack_from(">II", data, body + 12)
        unknown = 0xFFFFFFFF
    if timescale == 0 or duration == unknown:
        return Fraction(0)
    return Fraction(duration, timescale)


def _words(data: bytes, pos: int, count: int) -> array[int]:
    words = array(_WORD, data[pos : pos + 4 * count])
    if sys.byteorder == "little":
        words.byteswap()
    return words


class _Bits:
    def __init__(self, raw: bytes) -> None:
        self.value, self.left = int.from_bytes(raw, "big"), 8 * len(raw)

    def take(self, width: int) -> int:
        if width > self.left:
            raise AudioError("AAC configuration is truncated")
        self.left -= width
        return (self.value >> self.left) & ((1 << width) - 1)

    def object_type(self) -> int:
        kind = self.take(5)
        return 32 + self.take(6) if kind == 31 else kind

    def rate(self) -> int:
        index = self.take(4)
        if index == 15:
            return self.take(24)
        return _AAC_RATES[index] if index < len(_AAC_RATES) else 0


def _asc(config: bytes) -> tuple[int, int]:
    """(core sample rate, channels) of an AudioSpecificConfig (ISO/IEC 14496-3 1.6.2.1)."""
    bits = _Bits(config[:16])
    kind, rate, channels = bits.object_type(), bits.rate(), _AAC_CHANNELS.get(bits.take(4))
    if kind in (5, 29):  # explicit SBR or PS: the rate above is the core's; the core type follows
        bits.rate()
        kind = bits.object_type()
    if kind not in _AAC_TYPES or channels is None or not rate:
        raise AudioError("MP4 audio that is not AAC is not supported")
    return rate, channels


def _descriptor(data: bytes, pos: int, end: int, tag: int) -> tuple[int, int]:
    """(body start, body end) of the MPEG-4 descriptor with `tag` at `pos`."""
    if pos >= end or data[pos] != tag:
        raise AudioError("MP4 audio without an AAC decoder configuration")
    size, pos = 0, pos + 1
    for _ in range(4):
        byte = data[pos]
        size, pos = (size << 7) | (byte & 0x7F), pos + 1
        if not byte & 0x80:
            break
    if pos + size > end:
        raise AudioError("MP4 descriptor is truncated")
    return pos, pos + size


def _esds(data: bytes, body: int, end: int) -> tuple[int, int]:
    es, es_end = _descriptor(data, body + 4, end, 0x03)
    flags, pos = data[es + 2], es + 3
    pos += 2 if flags & 0x80 else 0  # dependsOn_ES_ID, then the URL, then OCR_ES_Id
    pos += 1 + data[pos] if flags & 0x40 else 0
    pos += 2 if flags & 0x20 else 0
    config, config_end = _descriptor(data, pos, es_end, 0x04)
    if data[config] not in _AAC_OTI:
        raise AudioError("MP4 audio that is not AAC is not supported")
    info, info_end = _descriptor(data, config + 13, config_end, 0x05)
    return _asc(data[info:info_end])


def _find_esds(data: bytes, start: int, end: int) -> tuple[int, int]:
    for kind, body, box_end in _boxes(data, start, end):
        if kind == b"esds":
            return _esds(data, body, box_end)
        if kind == b"wave":  # QuickTime wraps it one level down
            return _find_esds(data, body, box_end)
    raise AudioError("MP4 audio without an AAC decoder configuration")


def _stsd(data: bytes, body: int, end: int, track: _Track) -> None:
    for kind, entry, entry_end in _boxes(data, body + 8, end):
        if kind != b"mp4a":
            track.other_codec = True
            continue
        children = {0: 28, 1: 44, 2: 64}.get(struct.unpack_from(">H", data, entry + 8)[0])
        if children is None:
            raise AudioError("unknown MP4 sound description version")
        rate, channels = _find_esds(data, entry + children, entry_end)
        track.rate = min(track.rate, rate) if track.rate else rate
        track.channels = max(track.channels, channels)


def _stbl(data: bytes, body: int, end: int, track: _Track) -> None:
    for kind, box, box_end in _boxes(data, body, end):
        if kind == b"stsd":
            _stsd(data, box, box_end, track)
        elif kind == b"stz2":
            raise AudioError("compact MP4 sample sizes are not supported")
        elif kind == b"stsz":
            size, count = struct.unpack_from(">II", data, box + 4)
            if not size and box + 12 + 4 * count > box_end:
                raise AudioError("MP4 sample size table is truncated")
            track.samples, track.has_sizes = count, True
            track.sample_bytes = size * count if size else sum(_words(data, box + 12, count))


def _mdia(data: bytes, body: int, end: int, track: _Track) -> Fraction:
    """Fill `track` from a `mdia` box; return its declared media duration."""
    declared = Fraction(0)
    for kind, box, box_end in _boxes(data, body, end):
        if kind == b"mdhd":
            declared = _mp4_time(data, box)
        elif kind == b"hdlr":
            track.handler = data[box + 8 : box + 12]
        elif kind == b"minf":
            for name, stbl, stbl_end in _boxes(data, box, box_end):
                if name == b"stbl":
                    _stbl(data, stbl, stbl_end, track)
    return declared


def _trak(data: bytes, body: int, end: int) -> tuple[int, _Track, Fraction]:
    """(track id, what its tables count, declared media duration) of a `trak`."""
    track, track_id, declared = _Track(), 0, Fraction(0)
    for kind, box, box_end in _boxes(data, body, end):
        if kind == b"tkhd":
            track_id = struct.unpack_from(">I", data, box + (20 if data[box] == 1 else 12))[0]
        elif kind == b"mdia":
            declared = max(declared, _mdia(data, box, box_end, track))
    if track.handler == b"soun" and (track.other_codec or not track.rate):
        raise AudioError("MP4 audio that is not AAC is not supported")
    if track.handler == b"soun" and not track.has_sizes:
        raise AudioError("MP4 audio without a sample size table")
    return track_id, track, declared


def _moov(data: bytes, body: int, end: int, tracks: dict[int, _Track]) -> Fraction:
    """Read the tracks into `tracks`; return the largest declared duration."""
    declared, timescale, extends = Fraction(0), 0, []
    for kind, box, box_end in _boxes(data, body, end):
        if kind == b"mvhd":
            declared = max(declared, _mp4_time(data, box))
            timescale = struct.unpack_from(">I", data, box + (20 if data[box] == 1 else 12))[0]
        elif kind == b"trak":
            track_id, track, media = _trak(data, box, box_end)
            if track_id in tracks:
                raise AudioError("MP4 with two tracks of the same id")
            tracks[track_id], declared = track, max(declared, media)
        elif kind == b"mvex":
            extends.append((box, box_end))
    for box, box_end in extends:
        for kind, sub, _send in _boxes(data, box, box_end):
            if kind == b"mehd" and not timescale:
                raise AudioError("MP4 has no movie header")
            if kind == b"mehd":
                total = struct.unpack_from(">Q" if data[sub] == 1 else ">I", data, sub + 4)[0]
                declared = max(declared, Fraction(total, timescale))
            elif kind == b"trex" and (
                extended := tracks.get(struct.unpack_from(">I", data, sub + 4)[0])
            ):
                extended.default_size = struct.unpack_from(">I", data, sub + 16)[0]
    return declared


def _trun(data: bytes, body: int, end: int, default_size: int) -> tuple[int, int]:
    """(samples, bytes) of a fragment's track run."""
    flags, count = (
        int.from_bytes(data[body + 1 : body + 4], "big"),
        struct.unpack_from(">I", data, body + 4)[0],
    )
    pos = body + 8 + (4 if flags & 0x1 else 0) + (4 if flags & 0x4 else 0)
    fields = bin(flags & 0xF00).count("1")  # duration, size, flags, composition offset
    if pos + 4 * fields * count > end:
        raise AudioError("MP4 track run is truncated")
    if not flags & 0x200:
        return count, count * default_size
    words = _words(data, pos, fields * count)
    return count, sum(words[(1 if flags & 0x100 else 0) :: fields])


def _tfhd(data: bytes, box: int, tracks: dict[int, _Track]) -> tuple[_Track, int]:
    """The track a fragment belongs to and its default sample size."""
    flags = int.from_bytes(data[box + 1 : box + 4], "big")
    track = tracks.get(struct.unpack_from(">I", data, box + 4)[0])
    if track is None:
        raise AudioError("MP4 fragment for an undeclared track")
    if not flags & 0x10:
        return track, track.default_size
    at = box + 8 + (8 if flags & 0x1 else 0) + (4 if flags & 0x2 else 0) + (4 if flags & 0x8 else 0)
    return track, struct.unpack_from(">I", data, at)[0]


def _moof(data: bytes, body: int, end: int, tracks: dict[int, _Track]) -> None:
    for kind, traf, traf_end in _boxes(data, body, end):
        if kind != b"traf":
            continue
        track, default_size = None, 0
        for sub, box, box_end in _boxes(data, traf, traf_end):
            if sub == b"tfhd":
                track, default_size = _tfhd(data, box, tracks)
            elif sub == b"trun":
                if track is None:
                    raise AudioError("MP4 track run before its fragment header")
                samples, size = _trun(data, box, box_end, default_size)
                track.samples += samples
                track.sample_bytes += size


def mp4(data: bytes) -> Counted:
    tracks: dict[int, _Track] = {}
    declared, saw_moov, media = Fraction(0), False, 0
    for kind, body, end in _boxes(data, 0, len(data)):
        if kind == b"moov":
            saw_moov = True
            declared = max(declared, _moov(data, body, end, tracks))
        elif kind == b"mdat":
            media += end - body
    if not saw_moov:
        raise AudioError("MP4 has no moov box (not a finished file)")
    for kind, body, end in _boxes(data, 0, len(data)):
        if kind == b"moof":
            _moof(data, body, end, tracks)
    # Media data the tables do not describe is audio this parse cannot see: refuse it.
    if media - sum(t.sample_bytes for t in tracks.values()) > max(media // 50, 8192):
        raise AudioError("MP4 media data is larger than its sample tables describe")
    audio = [t for t in tracks.values() if t.handler == b"soun"]
    if not audio:
        raise AudioError("MP4 without an audio track")
    counted = sum((Fraction(t.samples * AAC_FRAME, t.rate) for t in audio), Fraction(0))
    payload = sum(t.sample_bytes for t in audio)
    capacity = sum(t.samples * AAC_FRAME_BYTES * t.channels for t in audio)
    return Counted(max(counted, declared), payload, capacity)
