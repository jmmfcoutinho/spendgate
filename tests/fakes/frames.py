"""Wire framing for streams: SSE text frames and the AWS event-stream binary framing."""

import json
import struct
import zlib
from collections.abc import Mapping

_PRELUDE = struct.Struct(">II")
_CRC = struct.Struct(">I")
_STRING_HEADER = 7  # event-stream header value type "string"


def sse(data: str, event: str | None = None) -> bytes:
    """One SSE event; `event` adds the named `event:` line (Responses, Messages)."""
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {data}\n\n".encode()


def sse_json(payload: Mapping[str, object], event: str | None = None) -> bytes:
    return sse(json.dumps(payload), event)


def _header(name: str, value: str) -> bytes:
    n, v = name.encode(), value.encode()
    return bytes([len(n)]) + n + bytes([_STRING_HEADER]) + struct.pack(">H", len(v)) + v


def eventstream_frame(event_type: str, payload: Mapping[str, object]) -> bytes:
    """One real AWS event-stream message (prelude CRC, headers, JSON payload, message CRC)."""
    headers = (
        _header(":event-type", event_type)
        + _header(":content-type", "application/json")
        + _header(":message-type", "event")
    )
    body = json.dumps(payload).encode()
    total = 16 + len(headers) + len(body)
    prelude = _PRELUDE.pack(total, len(headers))
    head = prelude + _CRC.pack(zlib.crc32(prelude))
    message = head + headers + body
    return message + _CRC.pack(zlib.crc32(message))


def decode_eventstream(data: bytes) -> list[tuple[str, dict[str, object]]]:
    """Inverse of `eventstream_frame` for tests; verifies both CRCs."""
    events: list[tuple[str, dict[str, object]]] = []
    pos = 0
    while pos < len(data):
        total, header_len = _PRELUDE.unpack_from(data, pos)
        (prelude_crc,) = _CRC.unpack_from(data, pos + 8)
        if zlib.crc32(data[pos : pos + 8]) != prelude_crc:
            raise ValueError("prelude CRC mismatch")
        message = data[pos : pos + total - 4]
        if zlib.crc32(message) != _CRC.unpack_from(data, pos + total - 4)[0]:
            raise ValueError("message CRC mismatch")
        headers = _decode_headers(data[pos + 12 : pos + 12 + header_len])
        payload = json.loads(data[pos + 12 + header_len : pos + total - 4])
        events.append((headers[":event-type"], payload))
        pos += total
    return events


def _decode_headers(raw: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    pos = 0
    while pos < len(raw):
        name_len = raw[pos]
        name = raw[pos + 1 : pos + 1 + name_len].decode()
        pos += 1 + name_len + 1  # skip the value-type byte (always string here)
        (value_len,) = struct.unpack_from(">H", raw, pos)
        out[name] = raw[pos + 2 : pos + 2 + value_len].decode()
        pos += 2 + value_len
    return out
