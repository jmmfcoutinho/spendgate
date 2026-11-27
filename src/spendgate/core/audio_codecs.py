"""What every audio container parser shares: the error, the counted result and codec frame sizes.

A container's header says how long it is; the decoder ignores that and decodes the packets. So
the parsers count the packets (ADR-0029), and these are the per-codec facts the counting needs.
"""

# ruff: noqa: PLR2004 - codec layouts are fixed-offset byte formats; the numbers are the spec
import struct
from dataclasses import dataclass
from fractions import Fraction

# Most packets, frames, blocks, boxes or sync candidates one file may have. The parse is
# synchronous on the event loop, so this bounds the stall on hostile input while leaving room for
# a real 25 MB upload (Opus at 6 kbit/s, 20 ms packets: about 1.7 M packets).
MAX_UNITS = 2_000_000


class AudioError(ValueError):
    """The audio cannot be measured with certainty."""


@dataclass(frozen=True, slots=True)
class Counted:
    """A length counted from the audio, and the size floor that backs it.

    `payload` is the bytes of audio the parser found; `capacity` is the most bytes the counted
    length can carry at the codec's highest bitrate. `measure` refuses a file whose payload is
    larger: its length was understated somewhere. None means the length is exact by construction.
    """

    seconds: Fraction
    payload: int = 0
    capacity: int | None = None


def _opus_frame_samples(config: int) -> int:
    """Samples at 48 kHz of one frame of the given TOC configuration (RFC 6716 section 3.1)."""
    if config < 12:  # SILK: 10, 20, 40, 60 ms
        return (480, 960, 1920, 2880)[config % 4]
    if config < 16:  # Hybrid: 10, 20 ms
        return (480, 960)[config % 2]
    return (120, 240, 480, 960)[config % 4]  # CELT: 2.5, 5, 10, 20 ms


OPUS_RATE = 48_000
OPUS_MAX_SAMPLES = 5760  # 120 ms: the longest packet RFC 6716 allows
OPUS_MAX_FRAMES = 48
# Most bytes one Opus frame may take (RFC 6716 section 3.2.1), plus its length bytes.
OPUS_FRAME_BYTES = 1277
_TOC_FRAMES = tuple(1 if (toc & 3) == 0 else 2 for toc in range(256))
_TOC_SAMPLES = tuple(_opus_frame_samples(toc >> 3) * _TOC_FRAMES[toc] for toc in range(256))


def opus_packet(data: bytes, start: int, length: int) -> tuple[int, int]:
    """(samples at 48 kHz, frames) of the Opus packet at `data[start:start + length]`.

    An empty or invalid packet is counted as the longest a packet can be, never as nothing: a
    decoder may conceal it with that much audio.
    """
    if length <= 0:
        return OPUS_MAX_SAMPLES, OPUS_MAX_FRAMES
    toc = data[start]
    if (toc & 3) != 3:
        return _TOC_SAMPLES[toc], _TOC_FRAMES[toc]
    frames = data[start + 1] & 0x3F if length > 1 else 0
    samples = frames * _opus_frame_samples(toc >> 3)
    if not frames or samples > OPUS_MAX_SAMPLES:
        return OPUS_MAX_SAMPLES, OPUS_MAX_FRAMES
    return samples, frames


def opus_streams(head: bytes) -> int:
    """Number of Opus streams per packet, from an `OpusHead` (RFC 7845 section 5.1)."""
    if len(head) < 19 or head[:8] != b"OpusHead":
        raise AudioError("malformed Opus header")
    if head[18] == 0:
        return 1
    if len(head) < 21 or not head[19]:
        raise AudioError("malformed Opus channel mapping")
    return head[19]


@dataclass(frozen=True, slots=True)
class Vorbis:
    rate: int
    channels: int
    per_packet: int  # most samples one audio packet can decode to (half the long block)


def vorbis_ident(packet: bytes) -> Vorbis:
    """The facts of a Vorbis identification header that bound how much audio a packet holds."""
    if len(packet) < 30 or packet[:7] != b"\x01vorbis":
        raise AudioError("malformed Vorbis header")
    channels, rate = packet[11], struct.unpack_from("<I", packet, 12)[0]
    short, long = packet[28] & 15, packet[28] >> 4
    if not rate:
        raise AudioError("Vorbis stream without a sample rate")
    if not channels or not 6 <= short <= long <= 13:
        raise AudioError("Vorbis header with invalid channels or block sizes")
    return Vorbis(rate, channels, (1 << long) // 2)
