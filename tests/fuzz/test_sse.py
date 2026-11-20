"""Target 3: `providers.sse.SseParser`.

Inputs: arbitrary bytes (SSE-flavoured fragments, invalid UTF-8, huge lines, every line ending)
under arbitrary chunk splits, against the default limits and tiny ones so every limit is hit.

Invariants: only `StreamBroken` / `StreamLimitExceeded` are raised; a stream that parses whole
parses identically under any split and one that fails whole fails under any split; the
internal buffer never exceeds `max_line_bytes` after a `feed`; no emitted event is larger than
`max_event_bytes`; events and stream bytes respect `SseLimits`.
"""

import itertools
from dataclasses import dataclass

from hypothesis import strategies as st

from spendgate.providers.port import SseEvent, SseLimits, StreamBroken
from spendgate.providers.sse import SseParser
from tests.fuzz.conftest import Case, run_fuzz

_PIECES = [
    b"data: ",
    b"data:",
    b"event: ",
    b"event:",
    b"id: 7",
    b"retry: 10",
    b":keep-alive",
    b"\n",
    b"\r\n",
    b"\r",
    b"\n\n",
    b"\r\n\r\n",
    b"\r\r",
    b"[DONE]",
    b'{"a":1}',
    b"\xff",
    b"\xc3",
    b"\xe2\x82\xac",
    b"\x00",
    b" ",
]

_STREAMS = st.one_of(
    st.lists(st.sampled_from(_PIECES) | st.binary(max_size=12), max_size=40).map(b"".join),
    st.binary(max_size=300),
    st.lists(st.sampled_from([b"data: x\n", b"event: e\n"]), max_size=30).map(
        lambda xs: b"".join(xs) + b"\n"
    ),
    st.integers(1, 400).map(lambda n: b"data: " + b"a" * n + b"\n\n"),
)

_LIMITS = st.one_of(
    st.just(SseLimits()),
    st.builds(
        SseLimits,
        max_line_bytes=st.integers(1, 40),
        max_event_bytes=st.integers(1, 120),
        max_events=st.integers(1, 6),
        max_stream_bytes=st.integers(1, 400),
    ),
)


@dataclass(frozen=True, slots=True)
class Run:
    events: tuple[SseEvent, ...]
    error: str | None  # the exception class name, None when the stream parsed to the end
    buffer_ok: bool


def run(limits: SseLimits, chunks: list[bytes]) -> Run:
    parser = SseParser(limits)
    events: list[SseEvent] = []
    buffer_ok = True
    try:
        for chunk in chunks:
            events += parser.feed(chunk)
            # a pending final CR is a terminator, not a line byte (FZ-04)
            held = len(parser._buf) - parser._buf.endswith(b"\r")
            buffer_ok &= held <= limits.max_line_bytes
        events += parser.close()
    except StreamBroken as exc:  # StreamLimitExceeded is a subclass; anything else escapes
        return Run(tuple(events), type(exc).__name__, buffer_ok)
    return Run(tuple(events), None, buffer_ok)


def split(data: bytes, cuts: list[int]) -> list[bytes]:
    points = sorted({c % (len(data) + 1) for c in cuts} | {0, len(data)})
    return [data[a:b] for a, b in itertools.pairwise(points)]


def check(case: tuple[bytes, SseLimits, list[int]]) -> None:
    data, limits, cuts = case
    whole = run(limits, [data])
    parts = run(limits, split(data, cuts))
    bytewise = run(limits, [data[i : i + 1] for i in range(len(data))])
    for result in (whole, parts, bytewise):
        assert result.buffer_ok, "the buffer grew past max_line_bytes"
        assert all(len(e.data.encode()) <= limits.max_event_bytes for e in result.events)
        assert len(result.events) <= limits.max_events
    if len(data) > limits.max_stream_bytes:
        assert whole.error is not None and parts.error is not None
    if whole.error is None:
        assert parts.error is None and bytewise.error is None, (parts.error, bytewise.error)
        assert parts.events == whole.events == bytewise.events
    else:
        assert parts.error is not None and bytewise.error is not None, whole.error


def check_serialise_roundtrip(events: list[SseEvent]) -> None:
    from spendgate.providers.sse import serialise  # noqa: PLC0415

    wire = b"".join(serialise(e) for e in events)
    out = run(SseLimits(), [wire])
    assert out.error is None
    assert list(out.events) == events


_NAMES = st.none() | st.text(
    st.characters(codec="utf-8", exclude_categories=["Cs", "Cc"]), min_size=1, max_size=10
)
_DATA = st.text(
    st.characters(codec="utf-8", exclude_categories=["Cs"], exclude_characters="\r"),
    min_size=1,
    max_size=30,
)


def test_fuzz_sse() -> None:
    run_fuzz(
        "sse",
        [
            Case(
                "split_invariance_and_limits",
                st.tuples(_STREAMS, _LIMITS, st.lists(st.integers(0, 10_000), max_size=8)),
                check,
            ),
            Case(
                "serialise_roundtrip",
                st.lists(st.builds(SseEvent, _NAMES, _DATA), max_size=6),
                check_serialise_roundtrip,
            ),
        ],
    )
