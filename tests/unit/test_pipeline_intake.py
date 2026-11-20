"""Step 1: the gate, the capped read, body parsing and the multipart parser."""

import asyncio
from collections.abc import AsyncIterator

from spendgate.core.errors import ErrorCode, Refusal
from spendgate.pipeline import intake
from spendgate.pipeline.model import CHAT, TRANSCRIPTIONS, Inbound
from spendgate.pipeline.multipart import MAX_PARTS, boundary_of, parse_multipart

BOUNDARY = "b0undary"
CT = f"multipart/form-data; boundary={BOUNDARY}"


def part(name: str, value: bytes, filename: str | None = None) -> bytes:
    extra = f'; filename="{filename}"' if filename else ""
    head = f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"{extra}\r\n\r\n'
    return head.encode() + value + b"\r\n"


def form(*parts: bytes) -> bytes:
    return b"".join(parts) + f"--{BOUNDARY}--\r\n".encode()


def refused(result: object) -> Refusal:
    assert isinstance(result, Refusal)
    return result


# --- the gate ---------------------------------------------------------------------------------


def test_gate_refuses_oversize_before_taking_a_slot() -> None:
    gate = intake.Gate(2, 1000, 10_000)
    assert refused(gate.enter(1001)).code is ErrorCode.REQUEST_TOO_LARGE
    assert gate.in_flight == 0


def test_gate_refuses_fast_when_full_and_frees_on_release() -> None:
    gate = intake.Gate(1, 1000, 10_000)
    slot = gate.enter(10)
    assert isinstance(slot, intake.Slot) and gate.in_flight == 1
    full = refused(gate.enter(10))
    assert full.code is ErrorCode.RATE_LIMITED and full.retry_after_seconds == 1
    slot.release()
    slot.release()  # idempotent: a double release never frees someone else's place
    assert gate.in_flight == 0 and isinstance(gate.enter(10), intake.Slot)


def test_gate_bounds_body_memory_and_charges_unknown_length_as_it_arrives() -> None:
    gate = intake.Gate(10, 1000, 1000 * intake.COPIES)  # room for exactly one maximal body
    first = gate.enter(None)  # no Content-Length: nothing is charged until bytes are buffered
    assert isinstance(first, intake.Slot)
    assert first.grow(1000) is None  # a maximal body fills the budget
    assert refused(gate.enter(1)).code is ErrorCode.RATE_LIMITED
    first.release()
    assert isinstance(gate.enter(1), intake.Slot)


def test_read_capped_reserves_memory_for_each_chunk_and_stops_when_refused() -> None:
    seen: list[int] = []

    def reserve(total: int) -> Refusal | None:
        seen.append(total)
        return None if total <= 4 else refused(intake.too_large(4))

    result = asyncio.run(
        intake.read_capped(chunks(b"ab", b"cd", b"e", b"never"), 100, reserve=reserve)
    )
    assert seen == [2, 4, 5] and refused(result).code is ErrorCode.REQUEST_TOO_LARGE


def test_content_length_must_be_a_number() -> None:
    assert intake.content_length({}) is None
    assert intake.content_length({"content-length": "12"}) == 12
    assert isinstance(intake.content_length({"content-length": "-1"}), Refusal)
    assert isinstance(intake.content_length({"content-length": "1e3"}), Refusal)


# --- capped read --------------------------------------------------------------------------------


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for p in parts:
        yield p


def test_read_capped_stops_at_the_limit() -> None:
    ok = asyncio.run(intake.read_capped(chunks(b"ab", b"cd"), 4))
    assert ok == bytearray(b"abcd")
    over = asyncio.run(intake.read_capped(chunks(b"ab", b"cde", b"never"), 4))
    assert refused(over).code is ErrorCode.REQUEST_TOO_LARGE


# --- body parsing ---------------------------------------------------------------------------------


def test_json_body_must_be_an_object_with_the_right_media_type() -> None:
    headers = {"content-type": "application/json; charset=utf-8"}
    parsed = intake.parse_body(CHAT, headers, b'{"model": "m"}')
    assert isinstance(parsed, Inbound) and parsed.body == {"model": "m"}
    assert refused(intake.parse_body(CHAT, headers, b"[1]")).code is ErrorCode.INVALID_REQUEST
    assert refused(intake.parse_body(CHAT, headers, b"{")).code is ErrorCode.INVALID_REQUEST
    assert refused(intake.parse_body(CHAT, {}, b"{}")).code is ErrorCode.INVALID_REQUEST
    assert (
        refused(intake.parse_body(TRANSCRIPTIONS, headers, b"{}")).code is ErrorCode.INVALID_REQUEST
    )


def test_wrong_media_type_is_415_and_other_body_faults_stay_400() -> None:  # protocol 2.1
    headers = {"content-type": "application/json"}
    wrongs = (
        intake.parse_body(CHAT, {}, b"{}"),
        intake.parse_body(CHAT, {"content-type": "text/plain"}, b"x"),
        intake.parse_body(TRANSCRIPTIONS, headers, b"{}"),
    )
    for wrong in wrongs:
        assert intake.status_of(refused(wrong)) == 415
    assert intake.status_of(refused(intake.parse_body(CHAT, headers, b"{"))) is None


def test_multipart_body_is_parsed_after_the_size_check() -> None:
    raw = form(part("model", b"whisper"), part("file", b"RIFF\r\n--x", "a.wav"))
    parsed = intake.parse_body(TRANSCRIPTIONS, {"content-type": CT}, raw)
    assert isinstance(parsed, Inbound)
    assert parsed.body == {"model": "whisper"} and parsed.files == {"file": b"RIFF\r\n--x"}


# --- multipart parser -------------------------------------------------------------------------


def test_multipart_fields_lists_and_file_bytes() -> None:
    raw = form(
        part("timestamp_granularities[]", b"word"),
        part("timestamp_granularities[]", b"segment"),
        part("prompt", "olá".encode()),
        part("file", bytes(range(256)), "x.bin"),
    )
    parsed = parse_multipart(CT, raw)
    assert not isinstance(parsed, Refusal)
    fields, files = parsed
    assert fields == {"timestamp_granularities[]": ["word", "segment"], "prompt": "olá"}
    assert files == {"file": bytes(range(256))}  # binary content survives, CR LF included


def test_multipart_accepts_a_quoted_boundary_and_a_bytearray() -> None:
    assert boundary_of('multipart/form-data; boundary="b0undary"') == b"b0undary"
    parsed = parse_multipart(
        f'multipart/form-data; boundary="{BOUNDARY}"', bytearray(form(part("a", b"1")))
    )
    assert not isinstance(parsed, Refusal) and parsed[0] == {"a": "1"}


def test_multipart_refuses_malformed_input() -> None:
    good = form(part("a", b"1"))
    cases = [
        ("multipart/form-data", good),  # no boundary
        (CT, b"junk" + good),  # no opening boundary
        (CT, good[:-6]),  # no closing boundary
        (CT, form(part("a", b"1"), part("a", b"2"))),  # repeated field
        (CT, form(part("file", b"1", "a"), part("file", b"2", "b"))),  # repeated file
        (CT, form(part("a", b"\xff\xfe"))),  # not UTF-8
        (CT, form(b"--b0undary\r\nX-Other: 1\r\n\r\nv\r\n")),  # no form-data name
        (
            CT,
            form(b'--b0undary\r\nContent-Disposition: form-data; name="a"\r\nv\r\n'),
        ),  # no head end
        (CT, f"--{BOUNDARY}xx\r\n".encode()),  # bad boundary line
    ]
    for content_type, raw in cases:
        assert refused(parse_multipart(content_type, raw)).code is ErrorCode.INVALID_REQUEST, raw


def test_multipart_limits() -> None:
    many = form(*[part(f"f{i}", b"x") for i in range(MAX_PARTS + 1)])
    assert refused(parse_multipart(CT, many)).code is ErrorCode.INVALID_REQUEST
    big = form(part("prompt", b"x" * (1024 * 1024 + 1)))
    assert refused(parse_multipart(CT, big)).code is ErrorCode.REQUEST_TOO_LARGE
