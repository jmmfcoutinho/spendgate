"""Target 4: `providers.bedrock_stream.EventStreamDecoder` and `_headers`.

Inputs: valid frames from a reference encoder (all header value types, correct CRCs), then mutated
two ways: raw bit flips / truncation (the CRC catches them) and structural mutations that recompute
both CRCs (so the decoder gets past the checksums into the length arithmetic and header parser).

Invariants: only `EventStreamError` is raised; decoding does not depend on how the bytes are split;
nothing over the frame limit is ever left in the buffer; valid frames round-trip to the same events;
`_headers` on arbitrary bytes raises only `EventStreamError`.
"""

import itertools
import struct
import zlib
from dataclasses import dataclass

from hypothesis import strategies as st

from spendgate.providers.bedrock_stream import (
    MAX_FRAME_BYTES,
    EventStreamDecoder,
    EventStreamError,
    _headers,
)
from spendgate.providers.port import SseEvent
from tests.fuzz.conftest import Case, run_fuzz

_FIXED = {0: 0, 1: 0, 2: 1, 3: 2, 4: 4, 5: 8, 8: 8, 9: 16}


def header(name: str, kind: int, value: bytes = b"") -> bytes:
    raw = name.encode()
    out = bytes([len(raw)]) + raw + bytes([kind])
    if kind in (6, 7):
        out += struct.pack(">H", len(value)) + value
    elif kind in _FIXED:
        out += value
    return out


def frame(
    headers: bytes, payload: bytes, *, total: int | None = None, hlen: int | None = None
) -> bytes:
    """A frame with correct CRCs over whatever lengths are written (the lengths may lie)."""
    length = 16 + len(headers) + len(payload) if total is None else total
    prelude = struct.pack(">II", length, len(headers) if hlen is None else hlen)
    head = prelude + struct.pack(">I", zlib.crc32(prelude))
    body = head + headers + payload
    return body + struct.pack(">I", zlib.crc32(body))


@dataclass(frozen=True, slots=True)
class Msg:
    kind: str  # "event" | "exception"
    name: str
    payload: str
    extra: bytes  # headers of other value types, which the decoder must skip


_TOKEN = st.from_regex(r"[A-Za-z][A-Za-z0-9-]{0,20}", fullmatch=True)


@st.composite
def extras(draw: st.DrawFn) -> bytes:
    out = b""
    for _ in range(draw(st.integers(0, 4))):
        kind = draw(st.sampled_from([0, 1, 2, 3, 4, 5, 6, 8, 9, 7]))
        value = (
            draw(st.binary(max_size=20))
            if kind in (6, 7)
            else draw(st.binary(min_size=_FIXED.get(kind, 0), max_size=_FIXED.get(kind, 0)))
        )
        if kind == 7:
            value = draw(
                st.text(st.characters(codec="utf-8", exclude_categories=["Cs"]), max_size=10)
            ).encode()
        out += header(draw(_TOKEN.map(lambda n: "x-" + n)), kind, value)
    return out


@st.composite
def messages(draw: st.DrawFn) -> Msg:
    kind = draw(st.sampled_from(["event", "event", "exception"]))
    payload = draw(st.text(st.characters(codec="utf-8", exclude_categories=["Cs"]), max_size=300))
    return Msg(kind, draw(_TOKEN), payload, draw(extras()))


def encode(msg: Msg) -> bytes:
    if msg.kind == "event":
        main = header(":message-type", 7, b"event") + header(":event-type", 7, msg.name.encode())
    else:
        main = header(":message-type", 7, b"exception") + header(
            ":exception-type", 7, msg.name.encode()
        )
    return frame(main + msg.extra, msg.payload.encode())


def expected(msg: Msg) -> SseEvent:
    name = msg.name if msg.kind == "event" else "exception:" + msg.name
    return SseEvent(name, msg.payload)


def decode(chunks: list[bytes], limit: int) -> tuple[list[SseEvent], str | None, bool]:
    decoder = EventStreamDecoder(limit)
    events: list[SseEvent] = []
    buffer_ok = True
    try:
        for chunk in chunks:
            events += decoder.feed(chunk)
            buffer_ok &= len(decoder._buf) <= max(limit, 11)
        decoder.close()
    except EventStreamError as exc:  # FrameTooLarge is a subclass; anything else escapes
        return events, type(exc).__name__, buffer_ok
    return events, None, buffer_ok


def split(data: bytes, cuts: list[int]) -> list[bytes]:
    points = sorted({c % (len(data) + 1) for c in cuts} | {0, len(data)})
    return [data[a:b] for a, b in itertools.pairwise(points)]


def check_roundtrip(case: tuple[list[Msg], list[int]]) -> None:
    msgs, cuts = case
    wire = b"".join(encode(m) for m in msgs)
    want = [expected(m) for m in msgs]
    for chunks in ([wire], split(wire, cuts), [wire[i : i + 1] for i in range(len(wire))]):
        events, error, buffer_ok = decode(chunks, MAX_FRAME_BYTES)
        assert (events, error, buffer_ok) == (want, None, True), error


@st.composite
def hostile(draw: st.DrawFn) -> tuple[bytes, int, list[int]]:
    msgs = draw(st.lists(messages(), min_size=1, max_size=4))
    wire = b"".join(encode(m) for m in msgs)
    limit = draw(st.sampled_from([MAX_FRAME_BYTES, 64, 200, 400, 12, 5]))
    cuts = draw(st.lists(st.integers(0, 10_000), max_size=6))
    mode = draw(st.sampled_from(["raw", "structural", "structural", "lengths", "headers"]))
    data = bytearray(wire)
    if mode == "raw":
        for _ in range(draw(st.integers(1, 4))):
            if not data:
                break
            at = draw(st.integers(0, len(data) - 1))
            op = draw(st.sampled_from(["flip", "cut", "insert", "set"]))
            if op == "flip":
                data[at] ^= 1 << draw(st.integers(0, 7))
            elif op == "cut":
                del data[at:]
            elif op == "insert":
                data[at:at] = draw(st.binary(max_size=30))
            else:
                data[at] = draw(st.sampled_from([0, 0x7F, 0x80, 0xFF]))
        return bytes(data), limit, cuts
    # Rebuild frames with valid CRCs around hostile content.
    out = b""
    for m in msgs:
        raw_headers = (
            header(":message-type", 7, b"event")
            + header(":event-type", 7, m.name.encode())
            + m.extra
        )
        payload = m.payload.encode()
        if mode == "headers":
            raw_headers = (
                draw(st.binary(max_size=60))
                if draw(st.booleans())
                else raw_headers + draw(st.binary(max_size=12))
            )
        total = hlen = None
        if mode == "lengths":
            total = draw(
                st.sampled_from(
                    [
                        0,
                        11,
                        12,
                        15,
                        16,
                        17,
                        2**31,
                        2**32 - 1,
                        16 + len(raw_headers) + len(payload) + draw(st.integers(-20, 20)),
                    ]
                )
            )
            hlen = draw(
                st.sampled_from([0, 1, 2**32 - 1, len(raw_headers) + draw(st.integers(-3, 3))])
            )
        if mode == "structural" and draw(st.booleans()):
            payload = draw(st.binary(max_size=80))
        out += frame(raw_headers, payload, total=total, hlen=hlen)
    return out, limit, cuts


def check_hostile(case: tuple[bytes, int, list[int]]) -> None:
    data, limit, cuts = case
    whole = decode([data], limit)
    parts = decode(split(data, cuts), limit)
    bytewise = decode([data[i : i + 1] for i in range(len(data))], limit)
    for _, _, buffer_ok in (whole, parts, bytewise):
        assert buffer_ok, "a frame over the limit was kept in the buffer"
    if whole[1] is None:
        assert parts[1] is None and bytewise[1] is None, (parts[1], bytewise[1])
        assert whole[0] == parts[0] == bytewise[0]
    else:
        assert parts[1] is not None and bytewise[1] is not None, whole[1]


def check_headers(raw: bytes) -> None:
    try:
        out = _headers(raw)
    except EventStreamError:
        return
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in out.items())


@st.composite
def header_soup(draw: st.DrawFn) -> bytes:
    if draw(st.booleans()):
        return draw(st.binary(max_size=80))
    parts = [
        header(
            draw(st.text(st.characters(codec="utf-8"), max_size=6)),
            draw(st.integers(0, 12)),
            draw(st.binary(max_size=18)),
        )
        for _ in range(draw(st.integers(0, 4)))
    ]
    return b"".join(parts) + draw(st.binary(max_size=3))


def test_fuzz_bedrock_stream() -> None:
    run_fuzz(
        "bedrock_stream",
        [
            Case(
                "valid_roundtrip_any_split",
                st.tuples(
                    st.lists(messages(), max_size=5), st.lists(st.integers(0, 10_000), max_size=6)
                ),
                check_roundtrip,
            ),
            Case("hostile_frames", hostile(), check_hostile),
            Case("headers_parser", header_soup(), check_headers),
        ],
    )
