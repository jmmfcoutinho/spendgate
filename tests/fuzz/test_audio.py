# ruff: noqa: PLR0911
"""Target 1: `core.audio.measure`, then `estimate(TRANSCRIPTION, ...)` (would have caught SR-03).

Inputs: valid WAV/MP3/MP4/Ogg/FLAC/WebM built by `tests/unit/audio_builders.py`, mutated (bit flips,
truncation, splices, extreme integers written over header fields), and raw bytes with or without a
real magic number in front.

Invariants: `measure` raises only `AudioError` or returns a `Measured` whose duration is positive
and at most `MAX_SECONDS`; it finishes in about 50 ms per MiB; an unmutated valid file with a known
duration is never measured shorter than the truth (the guard must not under-bill); `estimate` on the
same bytes returns only `Estimate` or `Refusal`, with audio seconds covering the measured duration.
"""

import time
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from hypothesis import strategies as st

from spendgate.core.audio import MAX_SECONDS, AudioError, measure
from spendgate.core.catalog import PriceEntry
from spendgate.core.cost import Estimate
from spendgate.core.errors import Refusal
from spendgate.core.estimate import RequestKind, estimate
from tests.fuzz.conftest import LONG, Case, run_fuzz
from tests.unit import audio_builders as fx

# Allowed wall time: ~50 ms per MiB plus a flat allowance for interpreter noise on a busy laptop.
_FLAT_S, _PER_MIB_S = 0.5, 0.05
_EXTREMES = (b"\x00", b"\x7f", b"\x80", b"\xff")


@dataclass(frozen=True, slots=True)
class Valid:
    data: bytes
    container: str
    true_ms: Fraction  # the real duration, never to be under-measured


def _entry(mode: str, meters: dict[str, object]) -> PriceEntry:
    return PriceEntry.model_validate(
        {
            "id": "openai/fuzz-model",
            "mode": mode,
            "currency": "USD",
            "effective_from": "2026-01-01",
            "source_url": "https://example.com/p",
            "verified": False,
            "meters": meters,
        }
    )


MINUTES = _entry("transcription", {"audio_input_per_minute": "0.006"})
TOKENS = _entry(
    "transcription",
    {"audio_input_per_mtok": "6", "input_per_mtok": "1.25", "output_per_mtok": "5"},
)


@st.composite
def valid_audio(draw: st.DrawFn) -> Valid:
    kind = draw(st.sampled_from(["wav", "mp3", "opus", "vorbis", "flac", "mp4", "webm"]))
    if kind == "wav":
        rate = draw(st.sampled_from([8000, 16000, 22050, 44100]))
        frames = draw(st.integers(1, 20 * rate))
        seconds = frames / rate
        return Valid(fx.wav(seconds, rate), "wav", Fraction(int(seconds * rate), rate) * 1000)
    if kind == "mp3":
        frames = draw(st.integers(3, 400))  # fewer than 3 frames is refused by design
        return Valid(
            fx.mp3(frames, id3=draw(st.sampled_from([0, 0, 10, 300]))),
            "mp3",
            Fraction(frames * 1152, 44100) * 1000,
        )
    if kind == "opus":
        seconds = draw(st.integers(1, 600)) / 10
        return Valid(fx.ogg_opus(seconds), "ogg", Fraction(int(seconds * 48000), 48000) * 1000)
    if kind == "vorbis":
        seconds = draw(st.integers(1, 600)) / 10
        return Valid(fx.ogg_vorbis(seconds), "ogg", Fraction(int(seconds * 44100), 44100) * 1000)
    if kind == "flac":
        seconds = draw(st.integers(1, 600)) / 10
        return Valid(fx.flac(seconds), "flac", Fraction(int(seconds * 44100), 44100) * 1000)
    if kind == "mp4":
        timescale = draw(st.sampled_from([1000, 600, 44100, 90000]))
        seconds = draw(st.integers(1, 3000)) / 10
        data = fx.mp4(seconds, timescale, moov_last=draw(st.booleans()))
        return Valid(data, "mp4", Fraction(int(seconds * timescale), timescale) * 1000)
    blocks = sorted(draw(st.lists(st.integers(0, 30_000), min_size=0, max_size=12)))
    ticks = draw(st.none() | st.integers(1, 90_000).map(float))
    if not blocks and ticks is None:
        blocks = [100]
    truth = max(ticks or 0.0, float(blocks[-1]) if blocks else 0.0)
    return Valid(fx.webm(blocks, duration_ticks=ticks), "webm", Fraction(truth))


@st.composite
def mutated(draw: st.DrawFn, base: bytes) -> bytes:
    data = bytearray(base)
    for _ in range(draw(st.integers(1, 6))):
        if not data:
            break
        op = draw(st.sampled_from(["flip", "extreme", "int", "truncate", "insert", "cut", "dup"]))
        at = draw(st.integers(0, len(data) - 1))
        if op == "flip":
            data[at] ^= 1 << draw(st.integers(0, 7))
        elif op == "extreme":
            span = draw(st.integers(1, 8))
            data[at : at + span] = draw(st.sampled_from(_EXTREMES)) * span
        elif op == "int":  # a whole field set to a boundary integer, either endianness
            value = draw(st.sampled_from([0, 1, 2**15, 2**31 - 1, 2**31, 2**32 - 1, 2**63 - 1]))
            size = draw(st.sampled_from([2, 4, 8]))
            raw = (value % (1 << (8 * size))).to_bytes(
                size, draw(st.sampled_from(["big", "little"]))
            )
            data[at : at + size] = raw
        elif op == "truncate":
            del data[at:]
        elif op == "insert":
            data[at:at] = draw(st.binary(max_size=64))
        elif op == "cut":
            del data[at : at + draw(st.integers(1, 64))]
        else:
            data += data[at : at + draw(st.integers(1, 256))]
    return bytes(data)


_MAGIC = (
    b"RIFF\x24\x00\x00\x00WAVE",
    b"OggS\x00\x02",
    b"fLaC",
    b"\x1a\x45\xdf\xa3",
    b"\x00\x00\x00\x18ftyp",
    b"ID3\x04\x00\x00",
    b"\xff\xfb\x90\x00",
)


@st.composite
def large_mp3(draw: st.DrawFn) -> bytes:
    """About a MiB of MP3 frames (valid, or with junk gaps that force resynchronisation)."""
    data = fx.mp3(draw(st.integers(1500, 2600)))
    if draw(st.booleans()):
        return draw(mutated(data))
    return data


audio_bytes = st.one_of(
    valid_audio().flatmap(lambda v: mutated(v.data)),
    valid_audio().map(lambda v: v.data),
    st.sampled_from(_MAGIC).flatmap(lambda m: st.binary(max_size=2000).map(lambda b: m + b)),
    st.binary(max_size=3000),
    st.sampled_from(_MAGIC).flatmap(
        lambda m: st.binary(max_size=40).map(lambda b: m + b"\x00" * 400 + b)
    ),
    large_mp3() if LONG else st.nothing(),
)


def _measure_checked(data: bytes) -> tuple[str, int] | None:
    """`measure` under the invariants; the (container, ms) or None when `AudioError`."""
    started = time.perf_counter()
    try:
        result = measure(data)
    except AudioError:
        result = None
    elapsed = time.perf_counter() - started
    allowed = _FLAT_S + _PER_MIB_S * len(data) / (1 << 20)
    assert elapsed <= allowed, f"measure took {elapsed:.3f}s for {len(data)} bytes"
    if result is None:
        return None
    assert 0 < result.duration_ms <= MAX_SECONDS * 1000, result
    return result.container, result.duration_ms


def check_measure(data: bytes) -> None:
    _measure_checked(data)


def check_estimate(data: bytes) -> None:
    measured = _measure_checked(data)
    for entry in (MINUTES, TOKENS):
        outcome = estimate(RequestKind.TRANSCRIPTION, {"file": data}, entry=entry)
        assert isinstance(outcome, Estimate | Refusal), outcome
        if isinstance(outcome, Estimate):
            assert measured is not None, "estimate accepted a file measure() refuses"
            assert outcome.audio_seconds * 1000 >= Decimal(measured[1]) - 1, (outcome, measured)
        elif measured is not None:
            raise AssertionError(f"measure() accepted but estimate refused: {outcome}")


def check_valid(valid: Valid) -> None:
    got = _measure_checked(valid.data)
    assert got is not None, f"a valid {valid.container} file was refused"
    assert got[0] == valid.container
    assert got[1] >= valid.true_ms, f"under-measured: {got[1]} ms < true {valid.true_ms} ms"


def test_fuzz_audio() -> None:
    run_fuzz(
        "audio",
        [
            Case("measure_raises_only_audio_error", audio_bytes, check_measure),
            Case("estimate_transcription", audio_bytes, check_estimate),
            Case("valid_file_never_under_measured", valid_audio(), check_valid),
        ],
    )
