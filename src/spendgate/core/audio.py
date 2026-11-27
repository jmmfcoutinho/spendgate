"""Audio duration counted from the bytes themselves (stdlib only, no decoding).

Transcription is priced by duration, so spendgate measures it before forwarding a byte and never
trusts what the caller says, nor what the file says about itself: a container's declared length
is a number its writer chose, while the provider decodes and bills every packet. So each parser
counts the audio a decoder will decode (MP3 frames, Ogg packets, FLAC frames, AAC samples in the
MP4 tables, Matroska blocks, PCM bytes), a declared length can only raise that count, and a size
floor backs it: a file holding more audio bytes than its counted length can carry at the codec's
highest bitrate is refused (ADR-0029).

Magic bytes decide the container, never a file name: WAV, MP3/MPEG audio, MP4/M4A (AAC), Ogg
(Opus, Vorbis), FLAC, WebM/Matroska (Opus, Vorbis). Anything else, anything truncated or
inconsistent, and anything whose duration would be a guess raises `AudioError`; the caller
refuses the request (`invalid_request`). Rounding is UP to the next millisecond; rounding to the
catalog's granularity is done once, in `core.cost`.
"""

# ruff: noqa: PLR2004 - container formats are fixed-offset byte layouts; the numbers are the spec
import struct
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction

from spendgate.core.audio_codecs import AudioError, Counted
from spendgate.core.audio_matroska import MAX_MATROSKA_ELEMENTS, matroska
from spendgate.core.audio_mp4 import mp4
from spendgate.core.audio_xiph import flac, ogg

__all__ = ["MAX_MATROSKA_ELEMENTS", "MAX_SECONDS", "AudioError", "Measured", "measure"]


@dataclass(frozen=True, slots=True)
class Measured:
    container: str  # wav | mp3 | mp4 | ogg | flac | webm
    duration_ms: int  # rounded up


MAX_SECONDS = 24 * 3600  # no transcription upload is longer; a bigger claim is junk (SR-03)


def _ms(counted: Counted) -> int:
    seconds = counted.seconds
    if seconds <= 0:
        raise AudioError("the audio has no duration")
    if seconds > MAX_SECONDS:
        raise AudioError("the audio claims a duration beyond 24 hours")
    # The size floor: more audio bytes than the counted length can carry at the codec's highest
    # bitrate means the length was understated somewhere. 2% and 8 KiB absorb framing overhead.
    if counted.capacity is not None and counted.payload > counted.capacity * 51 // 50 + 8192:
        raise AudioError("the audio holds more data than its length can carry")
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
            return Measured("ogg", _ms(ogg(data)))
        if head[:4] == b"fLaC":
            return Measured("flac", _ms(flac(data)))
        if head[:4] == b"\x1a\x45\xdf\xa3":
            return Measured("webm", _ms(matroska(data)))
        if head[4:8] == b"ftyp":
            return Measured("mp4", _ms(mp4(data)))
        if head[:3] == b"ID3" or head[0] == 0xFF:
            return Measured("mp3", _ms(_mp3(data)))
    except (struct.error, IndexError, ArithmeticError) as exc:
        raise AudioError("the audio file is truncated or malformed") from exc
    raise AudioError("unsupported or unrecognised audio format")


# --- WAV ---------------------------------------------------------------------------------------
# Format tag -> bytes per sample for a given bits per sample; 0 means not a valid combination.
_WAV_WIDTH: dict[int, Callable[[int], int]] = {
    1: lambda bits: (bits + 7) // 8 if 0 < bits <= 64 else 0,  # integer PCM
    3: lambda bits: bits // 8 if bits in (32, 64) else 0,  # IEEE float
    6: lambda bits: 1 if bits == 8 else 0,  # A-law
    7: lambda bits: 1 if bits == 8 else 0,  # mu-law
}


def _wav_format(data: bytes, body: int, size: int) -> tuple[int, int]:
    """(sample rate, bytes per frame) of a PCM `fmt ` chunk whose fields agree with each other.

    A decoder reads the samples with the rate, channels and sample width; `byte_rate` is a
    label it ignores. A label that disagrees is refused rather than believed.
    """
    if size < 16 or body + 16 > len(data):
        raise AudioError("WAV fmt chunk is truncated")
    tag, channels, rate, byte_rate, align, bits = struct.unpack_from("<HHIIHH", data, body)
    if tag == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE: the real tag opens the sub-format GUID
        if size < 40 or body + 40 > len(data):
            raise AudioError("WAV extensible fmt chunk is truncated")
        tag = struct.unpack_from("<H", data, body + 24)[0]
    width = _WAV_WIDTH.get(tag)
    if width is None:
        raise AudioError("only PCM, float, A-law and mu-law WAV are supported")
    if not rate or not 1 <= channels <= 64 or rate > 768_000 or not width(bits):
        raise AudioError("WAV with an unusable sample format")
    if align != channels * width(bits) or byte_rate != rate * align:
        raise AudioError("WAV format fields disagree with each other")
    return rate, align


def _is_chunk_id(tag: bytes) -> bool:
    return all(0x20 <= b < 0x7F for b in tag)


def _wav(data: bytes) -> Counted:
    pos, rate, frame, audio, saw_data = 12, 0, 0, 0, False
    while pos + 8 <= len(data):
        tag = data[pos : pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        if tag == b"data":
            if not frame:
                raise AudioError("WAV data before a usable fmt chunk")
            saw_data = True
            # 0 and 0xFFFFFFFF are the markers of a file written as a stream: use what is there.
            if size in (0, 0xFFFFFFFF) or body + size > len(data):
                audio += len(data) - body
                break
            audio += size
        elif saw_data and (not _is_chunk_id(tag) or body + size > len(data)):
            # Not a chunk after the data: a reader may take it as more samples, so count it.
            audio += len(data) - pos
            break
        elif tag == b"fmt ":
            if frame:
                raise AudioError("WAV with two fmt chunks")
            rate, frame = _wav_format(data, body, size)
        pos = body + size + (size & 1)
    if not saw_data:
        raise AudioError("WAV has no data chunk")
    return Counted(Fraction(audio - audio % frame, rate * frame))


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


def _mp3(data: bytes) -> Counted:
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
    return Counted(total)


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
