"""A minimal, strict multipart/form-data parser for the transcription upload (protocol 2.5).

Why our own: Starlette's form parser needs the `python-multipart` package (a sixth runtime
dependency) and spools files to temporary disk, after which the bytes would be read back into
memory anyway because the audio length is measured from them. This parser works on the already
size-capped body without copying it (`find` on the buffer, one copy per extracted part), accepts
only what the endpoint allows and refuses everything else with `invalid_request`.
"""

import re

from spendgate.core.errors import ErrorCode, Refusal
from spendgate.providers.port import Json

MAX_PARTS = 32
MAX_FIELD_BYTES = 1024 * 1024  # a text field (prompt) is never megabytes
_BOUNDARY = re.compile(r'boundary=(?:"([^"\r\n]{1,70})"|([^;\s"]{1,70}))', re.IGNORECASE)
_NAME = re.compile(r'(?:^|;)\s*name="([^"\r\n]{1,100})"', re.IGNORECASE)
_FILENAME = re.compile(r'(?:^|;)\s*filename="', re.IGNORECASE)
_HEAD_END = b"\r\n\r\n"
_CRLF = b"\r\n"

type Parsed = tuple[dict[str, Json], dict[str, bytes]]


def _bad(why: str, param: str | None = None) -> Refusal:
    return Refusal(ErrorCode.INVALID_REQUEST, f"Malformed multipart body: {why}.", param)


def boundary_of(content_type: str) -> bytes | None:
    match = _BOUNDARY.search(content_type)
    if match is None:
        return None
    return (match.group(1) or match.group(2)).encode("ascii", "ignore") or None


def _spans(data: bytes | bytearray, boundary: bytes) -> list[tuple[int, int]] | Refusal:
    """(start, end) of each part between the delimiters, with no byte of `data` copied."""
    marker = b"--" + boundary
    if not data.startswith(marker):
        return _bad("missing opening boundary")
    spans: list[tuple[int, int]] = []
    cursor = len(marker)
    while data[cursor : cursor + 2] != b"--":
        if data[cursor : cursor + 2] != _CRLF:
            return _bad("bad boundary line")
        start = cursor + 2
        end = data.find(_CRLF + marker, start)
        if end < 0:
            return _bad("missing closing boundary")
        spans.append((start, end))
        if len(spans) > MAX_PARTS:
            return _bad("too many parts")
        cursor = end + 2 + len(marker)
    return spans


def _head(data: bytes | bytearray, start: int, end: int) -> tuple[str, bool, int] | Refusal:
    """(field name, is a file, offset of the content) from a part's header block."""
    cut = data.find(_HEAD_END, start, end)
    if cut < 0:
        return _bad("part without headers")
    text = bytes(data[start:cut]).decode("latin-1")
    disposition = next(
        (
            line.partition(":")[2]
            for line in text.split("\r\n")
            if line.lower().startswith("content-disposition:")
        ),
        None,
    )
    named = _NAME.search(disposition) if disposition is not None else None
    if disposition is None or named is None or "form-data" not in disposition.lower():
        return _bad("part without a form-data name")
    return named.group(1), bool(_FILENAME.search(disposition)), cut + len(_HEAD_END)


def parse_multipart(content_type: str, data: bytes | bytearray) -> Parsed | Refusal:
    """Text fields (`name[]` repeated becomes a list) and file parts, or the refusal."""
    boundary = boundary_of(content_type)
    if boundary is None:
        return _bad("no boundary in Content-Type")
    spans = _spans(data, boundary)
    if isinstance(spans, Refusal):
        return spans
    fields: dict[str, Json] = {}
    files: dict[str, bytes] = {}
    view = memoryview(data)
    try:
        for start, end in spans:
            head = _head(data, start, end)
            if isinstance(head, Refusal):
                return head
            name, is_file, body_at = head
            if is_file:
                if name in files:
                    return _bad("repeated file part", name)
                files[name] = bytes(view[body_at:end])
            elif (refusal := _add_field(fields, name, view[body_at:end])) is not None:
                return refusal
    finally:
        view.release()
    return fields, files


def _add_field(fields: dict[str, Json], name: str, raw: memoryview) -> Refusal | None:
    if len(raw) > MAX_FIELD_BYTES:
        return Refusal(ErrorCode.REQUEST_TOO_LARGE, f"Form field {name} is too large.", name)
    try:
        value = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return _bad("a field is not UTF-8", name)
    if not name.endswith("[]"):
        if name in fields:
            return _bad("repeated field", name)
        fields[name] = value
        return None
    current = fields.setdefault(name, [])
    if isinstance(current, list):
        current.append(value)
    return None
