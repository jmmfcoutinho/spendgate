"""Target 2: `pipeline.intake.parse_body` and `pipeline.multipart.parse_multipart` (SR-07).

Inputs: arbitrary JSON, deeply nested JSON (up to the interpreter's own limit), raw bytes, and
multipart bodies from a reference encoder that are then mutated (boundary inside the content,
missing CRLFs, repeated names, more than `MAX_PARTS` parts, oversized and non-UTF-8 fields).

Invariants: never raises; parts never exceed `MAX_PARTS`; no field exceeds `MAX_FIELD_BYTES`; a JSON
body that is accepted is an object; a well-formed body from the reference encoder round-trips
exactly (or is refused when it has more than `MAX_PARTS` parts / an oversized field).
"""

import json
import re
from dataclasses import dataclass

from hypothesis import strategies as st

from spendgate.core.errors import Refusal
from spendgate.pipeline.intake import parse_body
from spendgate.pipeline.model import CHAT, TRANSCRIPTIONS, Inbound
from spendgate.pipeline.multipart import MAX_FIELD_BYTES, MAX_PARTS, parse_multipart
from tests.fuzz.conftest import Case, run_fuzz

_NAME = st.from_regex(r"[A-Za-z][A-Za-z0-9_.-]{0,20}", fullmatch=True)
_TEXT = st.text(st.characters(codec="utf-8", exclude_categories=["Cs"]), max_size=200)

json_values = st.recursive(
    st.none() | st.booleans() | st.integers(-(2**70), 2**70) | st.floats() | _TEXT,
    lambda inner: st.lists(inner, max_size=5) | st.dictionaries(_TEXT, inner, max_size=5),
    max_leaves=40,
)


def _dumps(value: object) -> bytes:
    return json.dumps(value, allow_nan=True).encode()


@st.composite
def json_bodies(draw: st.DrawFn) -> bytes:
    kind = draw(st.sampled_from(["object", "any", "deep_list", "deep_object", "mixed", "raw"]))
    if kind == "object":
        return _dumps(draw(st.dictionaries(_TEXT, json_values, max_size=6)))
    if kind == "any":
        return _dumps(draw(json_values))
    depth = draw(st.sampled_from([10, 100, 500, 990, 5_000, 100_000]))
    if kind == "deep_list":
        return b"[" * depth + b"]" * draw(st.sampled_from([0, depth, depth]))
    if kind == "deep_object":
        return b'{"a":' * depth + b"1" + b"}" * draw(st.sampled_from([0, depth, depth]))
    if kind == "mixed":
        return (
            b'{"messages":'
            + b'[{"x":' * min(depth, 5_000)
            + b"0"
            + b"}]" * min(depth, 5_000)
            + b"}"
        )
    return draw(
        st.binary(max_size=400)
        | st.sampled_from([b"", b"\xef\xbb\xbf{}", b"{}\x00", b"NaN", b"{'a':1}"])
    )


_CONTENT_TYPES = st.sampled_from(
    [
        "application/json",
        "Application/JSON; charset=utf-8",
        "text/plain",
        "",
        "multipart/form-data",
        "application/json;",
        " application/json ",
    ]
)


def check_json(case: tuple[str, bytes]) -> None:
    content_type, raw = case
    for endpoint in (CHAT, TRANSCRIPTIONS):
        out = parse_body(endpoint, {"content-type": content_type}, raw)
        assert isinstance(out, Inbound | Refusal), out
        if isinstance(out, Inbound):
            assert isinstance(out.body, dict) or endpoint.multipart
            assert len(out.files) <= MAX_PARTS


# --- multipart ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Form:
    fields: list[tuple[str, str]]  # (name, text) in wire order; `name[]` may repeat
    files: list[tuple[str, bytes]]
    boundary: str


def encode(form: Form, *, closing: bool = True) -> bytes:
    """The reference encoder: RFC 7578 with CRLF line ends."""
    out = bytearray()
    for name, text in form.fields:
        out += (
            f'--{form.boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        out += text.encode() + b"\r\n"
    for name, data in form.files:
        out += (
            f'--{form.boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            'filename="a.wav"\r\nContent-Type: audio/wav\r\n\r\n'
        ).encode()
        out += data + b"\r\n"
    out += f"--{form.boundary}--\r\n".encode() if closing else b""
    return bytes(out)


def _expected(form: Form) -> tuple[dict[str, object], dict[str, bytes]]:
    fields: dict[str, object] = {}
    for name, text in form.fields:
        if name.endswith("[]"):
            current = fields.setdefault(name, [])
            assert isinstance(current, list)
            current.append(text)
        else:
            fields[name] = text
    return fields, dict(form.files)


_BOUNDARY = st.from_regex(r"[A-Za-z0-9'()+_,./:=?-]{1,70}", fullmatch=True)


@st.composite
def forms(draw: st.DrawFn, *, many: bool = False) -> Form:
    """Well-formed forms: unique plain names, repeatable `name[]`, text without the delimiter."""
    boundary = draw(_BOUNDARY)
    delimiter = "--" + boundary
    safe = _TEXT.filter(lambda t: delimiter not in t)
    plain = draw(st.lists(_NAME, unique=True, max_size=40 if many else 6))
    listed = draw(st.lists(_NAME.map(lambda n: n + "[]"), max_size=3 if not many else 6))
    fields = [(n, draw(safe)) for n in plain]
    for name in listed:
        fields += [(name, draw(safe)) for _ in range(draw(st.integers(1, 4)))]
    if draw(st.integers(0, 15)) == 0:  # a field at the size limit
        size = draw(st.sampled_from([MAX_FIELD_BYTES - 1, MAX_FIELD_BYTES, MAX_FIELD_BYTES + 1]))
        fields.append(("big", "a" * size))
    names = {n for n, _ in fields}
    file_names = draw(st.lists(_NAME.filter(lambda n: n not in names), unique=True, max_size=3))
    payload = st.binary(max_size=300).filter(lambda b: delimiter.encode() not in b)
    files = [(n, draw(payload)) for n in file_names]
    return Form(draw(st.permutations(fields)) if fields else [], files, boundary)


def check_roundtrip(form: Form) -> None:
    body = encode(form)
    out = parse_multipart(f"multipart/form-data; boundary={form.boundary}", body)
    parts = len(form.fields) + len(form.files)
    oversized = any(len(t.encode()) > MAX_FIELD_BYTES for _, t in form.fields)
    if parts > MAX_PARTS or oversized:
        assert isinstance(out, Refusal), "a body over the part/field limit was accepted"
        return
    assert not isinstance(out, Refusal), (out, parts)
    assert (out[0], out[1]) == _expected(form)
    inbound = parse_body(
        TRANSCRIPTIONS, {"content-type": f"multipart/form-data; boundary={form.boundary}"}, body
    )
    assert isinstance(inbound, Inbound)
    assert (dict(inbound.body), dict(inbound.files)) == _expected(form)


@st.composite
def hostile_multipart(draw: st.DrawFn) -> tuple[str, bytes]:
    form = draw(forms(many=True))
    body = bytearray(encode(form, closing=draw(st.booleans())))
    content_type = f"multipart/form-data; boundary={form.boundary}"
    for _ in range(draw(st.integers(0, 5))):
        op = draw(
            st.sampled_from(
                [
                    "flip",
                    "drop_crlf",
                    "boundary_in_content",
                    "repeat_part",
                    "cut",
                    "insert",
                    "lone_lf",
                    "ct",
                ]
            )
        )
        at = draw(st.integers(0, max(len(body) - 1, 0)))
        if op == "flip" and body:
            body[at] ^= 1 << draw(st.integers(0, 7))
        elif op == "drop_crlf":
            i = body.find(b"\r\n", at)
            if i >= 0:
                del body[i : i + draw(st.integers(1, 2))]
        elif op == "boundary_in_content":
            body[at:at] = (
                b"\r\n--"
                + form.boundary.encode()
                + draw(st.sampled_from([b"", b"--", b"x", b"\r\n"]))
            )
        elif op == "repeat_part":
            body[at:at] = bytes(body[: draw(st.integers(0, len(body)))])
        elif op == "cut":
            del body[at:]
        elif op == "insert":
            body[at:at] = draw(st.binary(max_size=40))
        elif op == "lone_lf":
            body[:] = bytes(body).replace(b"\r\n", b"\n", draw(st.integers(1, 5)))
        else:
            content_type = draw(
                st.sampled_from(
                    [
                        "multipart/form-data",
                        'multipart/form-data; boundary=""',
                        "multipart/form-data; boundary=" + "x" * 80,
                        'multipart/form-data; boundary="a b"',
                        "multipart/form-data; boundary=a;boundary=b",
                        content_type.upper(),
                    ]
                )
            )
    return content_type, bytes(body)


def check_hostile(case: tuple[str, bytes]) -> None:
    content_type, body = case
    out = parse_multipart(content_type, body)
    assert isinstance(out, (Refusal, tuple)), out
    if isinstance(out, tuple):
        fields, files = out
        listed = sum(len(v) if isinstance(v, list) else 1 for v in fields.values())
        assert listed + len(files) <= MAX_PARTS, (listed, len(files))
        for value in fields.values():
            for text in value if isinstance(value, list) else [value]:
                assert isinstance(text, str) and len(text.encode()) <= MAX_FIELD_BYTES
        assert all(isinstance(k, str) and re.fullmatch(r'[^"\r\n]{1,100}', k) for k in fields)
        assert not set(files) & set(fields)
    whole = parse_body(TRANSCRIPTIONS, {"content-type": content_type}, bytearray(body))
    assert isinstance(whole, Inbound | Refusal)


def check_raw_multipart(raw: bytes) -> None:
    for content_type in ("multipart/form-data; boundary=x", 'multipart/form-data; boundary="--"'):
        out = parse_multipart(content_type, raw)
        assert isinstance(out, Refusal | tuple)
    assert isinstance(parse_body(TRANSCRIPTIONS, {}, raw), Refusal)


def test_fuzz_intake() -> None:
    run_fuzz(
        "intake",
        [
            Case("json_bodies", st.tuples(_CONTENT_TYPES, json_bodies()), check_json),
            Case("multipart_roundtrip", forms(many=True), check_roundtrip),
            Case("multipart_hostile", hostile_multipart(), check_hostile),
            Case(
                "multipart_raw_bytes",
                st.binary(max_size=500)
                | st.just(b"--x\r\n").flatmap(
                    lambda p: st.binary(max_size=300).map(lambda b: p + b)
                ),
                check_raw_multipart,
            ),
        ],
    )
