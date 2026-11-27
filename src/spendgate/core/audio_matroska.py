"""WebM / Matroska length, counted from the audio blocks (ADR-0029).

The declared Duration and the cluster and block timecodes are the writer's numbers; a decoder
decodes every block of the audio track. Opus blocks are counted from their TOC byte, Vorbis
blocks at the most one packet can decode to; the timecodes and Duration can only raise that.
"""

# ruff: noqa: PLR2004 - container formats are fixed-offset byte layouts; the numbers are the spec
import math
import struct
from dataclasses import dataclass
from fractions import Fraction

from spendgate.core.audio_codecs import (
    OPUS_FRAME_BYTES,
    OPUS_MAX_FRAMES,
    OPUS_MAX_SAMPLES,
    OPUS_RATE,
    AudioError,
    Counted,
    opus_packet,
    opus_streams,
    vorbis_ident,
)

# Most elements one Matroska file may have. The parse is synchronous on the event loop, so this
# bounds the stall (a 24 MiB file of empty Void elements took seconds, NF-05) while leaving room
# for a real 24 MiB Opus file (about 600k blocks).
MAX_MATROSKA_ELEMENTS = 1_000_000

_EBML, _SEGMENT, _INFO, _CLUSTER, _BLOCKGROUP = 0x1A45DFA3, 0x18538067, 0x1549A966, 0x1F43B675, 0xA0
_TIMESCALE, _DURATION, _CL_TIME, _SIMPLEBLOCK, _BLOCK, _BLOCKDUR = (
    0x2AD7B1,
    0x4489,
    0xE7,
    0xA3,
    0xA1,
    0x9B,
)
_TRACKS, _TRACKENTRY, _TRACKNUMBER, _CODECID, _CODECPRIVATE, _ENCODINGS = (
    0x1654AE6B,
    0xAE,
    0xD7,
    0x86,
    0x63A2,
    0x6D80,
)
_DESCEND = frozenset({_SEGMENT, _INFO, _CLUSTER, _BLOCKGROUP, _TRACKS})
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


@dataclass(slots=True)
class _Track:
    rate: int
    per_block: int  # Vorbis: most samples a packet decodes to; 0 for Opus (read its TOC)
    capacity_unit: int  # Opus: bytes per frame for all streams; Vorbis: bytes per sample
    samples: int = 0
    frames: int = 0
    payload: int = 0


def _xiph_first(private: bytes) -> bytes:
    """The first of the Xiph-laced header packets in a Vorbis CodecPrivate."""
    if len(private) < 3 or private[0] != 2:
        raise AudioError("malformed Vorbis CodecPrivate")
    pos, size = 1, 0
    while pos < len(private) and private[pos] == 255:
        size += 255
        pos += 1
    if pos >= len(private):
        raise AudioError("malformed Vorbis CodecPrivate")
    size += private[pos]
    pos += 1
    while pos < len(private) and private[pos] == 255:  # the second packet's size
        pos += 1
    return private[pos + 1 : pos + 1 + size]


def _audio_track(codec: bytes, private: bytes) -> _Track:
    if codec == b"A_OPUS":
        streams = opus_streams(private) if private else 1  # no OpusHead: decoders assume one
        return _Track(OPUS_RATE, 0, OPUS_FRAME_BYTES * streams)
    if codec == b"A_VORBIS":
        info = vorbis_ident(_xiph_first(private))
        return _Track(info.rate, info.per_packet, info.channels * 4)
    raise AudioError("only Opus and Vorbis audio in WebM or Matroska is supported")


def _track_entry(data: bytes, body: int, end: int) -> tuple[int, _Track | None, int]:
    """(track number, the audio track or None for another kind, elements read) of a TrackEntry."""
    number, codec, private, encoded = 0, b"", b"", False
    pos, elements = body, 0
    while pos < end:
        elements += 1
        ident, id_len, _ = _vint(data, pos, keep_marker=True)
        size, size_len, unknown = _vint(data, pos + id_len, keep_marker=False)
        inner = pos + id_len + size_len
        if unknown or inner + size > end:
            raise AudioError("malformed Matroska track entry")
        if ident == _TRACKNUMBER:
            number = _uint(data, inner, size)
        elif ident == _CODECID:
            codec = data[inner : inner + size].rstrip(b"\x00")
        elif ident == _CODECPRIVATE:
            private = data[inner : inner + size]
        elif ident == _ENCODINGS:
            encoded = True  # compressed or encrypted blocks: the TOC is not where we read it
        pos = inner + size
    if not number or not codec:
        raise AudioError("Matroska track without a number or codec")
    if not codec.startswith(b"A_"):
        return number, None, elements
    if encoded:
        raise AudioError("Matroska audio with content encoding is not supported")
    return number, _audio_track(codec, private), elements


def _count_block(track: _Track, data: bytes, start: int, end: int) -> None:
    """Count one block's frames; `start` is the block's flags byte."""
    if start >= end:
        raise AudioError("Matroska block is truncated")
    flags = data[start]
    laced = (flags >> 1) & 3
    frames, payload = 1, start + 1
    if laced:
        if payload >= end:
            raise AudioError("Matroska laced block is truncated")
        frames, payload = data[payload] + 1, payload + 1
    track.payload += end - payload
    if track.per_block:
        track.samples += frames * track.per_block
    elif laced:  # each laced Opus packet is counted at the longest a packet can be
        track.samples += frames * OPUS_MAX_SAMPLES
        track.frames += frames * OPUS_MAX_FRAMES
    else:
        samples, opus_frames = opus_packet(data, payload, end - payload)
        track.samples += samples
        track.frames += opus_frames


@dataclass(slots=True)
class _Scan:
    """What the walk has seen so far."""

    doctype_ok: bool = False
    scale: int = 1_000_000
    declared: Fraction = Fraction(0)
    cluster_time: int = 0
    end_ticks: int = 0
    saw_block: bool = False
    saw_audio: bool = False


def _block(scan: _Scan, tracks: dict[int, _Track | None], data: bytes, body: int, end: int) -> None:
    number, number_len, _ = _vint(data, body, keep_marker=False)
    if number not in tracks:
        raise AudioError("Matroska block for an undeclared track")
    relative = struct.unpack_from(">h", data, body + number_len)[0]
    scan.end_ticks = max(scan.end_ticks, scan.cluster_time + relative)
    scan.saw_block = True
    track = tracks[number]
    if track is not None:
        _count_block(track, data, body + number_len + 2, end)


def _leaf(scan: _Scan, ident: int, data: bytes, body: int, size: int) -> None:
    if ident == _TIMESCALE:
        scan.scale = _uint(data, body, size)
    elif ident == _DURATION and size in (4, 8):
        raw = struct.unpack(">f" if size == 4 else ">d", data[body : body + size])[0]
        if math.isfinite(raw) and 0 < raw < 1e15:  # NaN, inf and absurd values are ignored
            scan.declared = Fraction(raw)
    elif ident == _CL_TIME:
        scan.cluster_time = _uint(data, body, size)
    elif ident == _BLOCKDUR:
        scan.end_ticks = max(scan.end_ticks, scan.cluster_time + _uint(data, body, size))


def matroska(data: bytes) -> Counted:
    pos, elements = 0, 0
    scan = _Scan()
    tracks: dict[int, _Track | None] = {}
    while pos < len(data):
        elements += 1
        if elements > MAX_MATROSKA_ELEMENTS:
            raise AudioError("the Matroska file has too many elements")
        ident, id_len, _ = _vint(data, pos, keep_marker=True)
        size, size_len, unknown = _vint(data, pos + id_len, keep_marker=False)
        body = pos + id_len + size_len
        if ident == _EBML:
            head = data[body : body + size]
            scan.doctype_ok = b"webm" in head or b"matroska" in head
        elif ident in _DESCEND:
            pos = body  # children follow inline; unknown sizes need no special handling
            continue
        elif unknown:
            raise AudioError("unknown-size element where a size is required")
        elif body + size > len(data):
            break  # a truncated last element: the blocks before it still count
        elif ident == _TRACKENTRY:
            number, track, read = _track_entry(data, body, body + size)
            if number in tracks:
                raise AudioError("Matroska with two tracks of the same number")
            tracks[number] = track
            scan.saw_audio = scan.saw_audio or track is not None
            elements += read
        elif ident in (_SIMPLEBLOCK, _BLOCK):
            _block(scan, tracks, data, body, body + size)
        else:
            _leaf(scan, ident, data, body, size)
        pos = body + size
    if not scan.doctype_ok:
        raise AudioError("not a WebM or Matroska file")
    if not scan.saw_audio:
        raise AudioError("WebM or Matroska without an audio track")
    return _matroska_counted(scan, [t for t in tracks.values() if t is not None])


def _matroska_counted(scan: _Scan, tracks: list[_Track]) -> Counted:
    if scan.scale <= 0:
        raise AudioError("invalid Matroska timecode scale")
    seconds_per_tick = Fraction(scan.scale, 1_000_000_000)
    timed = scan.end_ticks * seconds_per_tick + _TAIL_MARGIN if scan.saw_block else Fraction(0)
    counted = sum((Fraction(t.samples, t.rate) for t in tracks), Fraction(0))
    capacity = sum((t.samples if t.per_block else t.frames) * t.capacity_unit for t in tracks)
    payload = sum(t.payload for t in tracks)
    return Counted(max(scan.declared * seconds_per_tick, timed, counted), payload, capacity)
