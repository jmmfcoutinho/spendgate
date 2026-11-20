"""Bedrock ConverseStream: AWS event-stream decoding, usage reading and OpenAI chunk output.

Kept apart from `bedrock.py` for size. It imports only `core` and `providers.port`.
The binary framing is AWS's: a 12-byte prelude (total length, header length, CRC-32 of those
8 bytes), the headers, the payload and a CRC-32 of everything before it. Every frame is
verified and size-limited before its payload is read, so a corrupt or hostile stream stops
early instead of being relayed (threat-model C-22). `BedrockStream` is the `StreamRelay`.
"""

import json
import struct
import zlib
from collections.abc import Mapping
from typing import Final

from spendgate.core.cost import Usage
from spendgate.providers.port import (
    UNREADABLE,
    Json,
    SseEvent,
    SseLimits,
    StreamBroken,
    StreamLimitExceeded,
    bounded,
    count_of,
    load_json,
)
from spendgate.providers.sse import StreamMeter, serialise

MAX_FRAME_BYTES: Final = 1024 * 1024  # a Converse event is a few KB; 1 MiB is generous
_PRELUDE = struct.Struct(">II")
_CRC = struct.Struct(">I")
_PRELUDE_LEN = 12
_FRAME_OVERHEAD = 16  # prelude (12) plus the trailing message CRC (4)
# value type -> fixed byte size; strings (7) and byte arrays (6) carry a 2-byte length instead
_FIXED_SIZE = {0: 0, 1: 0, 2: 1, 3: 2, 4: 4, 5: 8, 8: 8, 9: 16}
_STRING, _BYTES = 7, 6


class EventStreamError(StreamBroken, ValueError):
    """The stream is not valid event-stream framing; the pipeline settles it as `stream_broken`."""


class FrameTooLarge(EventStreamError, StreamLimitExceeded):
    """A frame longer than the event limit: both a framing error and a crossed limit."""


def as_obj(value: Json) -> Mapping[str, Json]:
    return value if isinstance(value, Mapping) else {}


def _count(raw: Mapping[str, Json], key: str) -> int | None:
    return count_of(raw.get(key))


def converse_usage(raw: Json) -> Usage | None:
    """Normalise a Converse `usage` object; None (charge the hold) when it is not trustworthy.

    Bedrock reports cache reads and writes beside `inputTokens`, not inside it. `Usage` counts
    them as subsets of the input, so they are added here: if a model ever reports them inside
    `inputTokens`, the result over-states cost slightly, never under-states it.
    """
    obj = as_obj(raw)
    inp, out = _count(obj, "inputTokens"), _count(obj, "outputTokens")
    if inp is None or out is None:
        return None
    read, write = _count(obj, "cacheReadInputTokens"), _count(obj, "cacheWriteInputTokens")
    if (read is None and "cacheReadInputTokens" in obj) or (
        write is None and "cacheWriteInputTokens" in obj
    ):
        return None
    read, write = read or 0, write or 0
    return bounded(
        Usage(
            input_tokens=inp + read + write,
            output_tokens=out,
            cached_input_tokens=read,
            cache_write_tokens=write,
        )
    )


def openai_usage(usage: Usage) -> Json:
    return {
        "prompt_tokens": usage.input_tokens,
        "completion_tokens": usage.output_tokens,
        "total_tokens": usage.input_tokens + usage.output_tokens,
        "prompt_tokens_details": {"cached_tokens": usage.cached_input_tokens},
    }


_FINISH = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "guardrail_intervened": "content_filter",
    "content_filtered": "content_filter",
}


def finish_reason(stop_reason: Json) -> str:
    return _FINISH.get(stop_reason, "stop") if isinstance(stop_reason, str) else "stop"


def dumps(payload: Mapping[str, Json]) -> str:
    return json.dumps(payload, separators=(",", ":"))


class EventStreamDecoder:
    """Incremental decoder: `feed` bytes in any split, get the complete frames as `SseEvent`s.

    A frame's `:event-type` becomes `SseEvent.event` and its JSON payload text becomes `data`.
    Exception frames come out as `exception:<type>` so the caller never mistakes them for data.
    """

    def __init__(self, max_frame_bytes: int = MAX_FRAME_BYTES) -> None:
        self._max = max_frame_bytes
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[SseEvent]:
        self._buf += chunk
        events: list[SseEvent] = []
        while (frame := self._next_frame()) is not None:
            events.append(frame)
        return events

    def close(self) -> None:
        """End of stream: leftover bytes mean the upstream cut a frame in half."""
        if self._buf:
            raise EventStreamError("stream ended inside a frame")

    def _next_frame(self) -> SseEvent | None:
        buf = self._buf
        if len(buf) < _PRELUDE_LEN:
            return None
        total, header_len = _PRELUDE.unpack_from(buf)
        if zlib.crc32(buf[:8]) != _CRC.unpack_from(buf, 8)[0]:
            raise EventStreamError("prelude CRC mismatch")
        # Checked before waiting for the rest, so an oversize frame is never buffered.
        if total > self._max:
            raise FrameTooLarge("frame length out of bounds")
        if total < _FRAME_OVERHEAD + header_len:
            raise EventStreamError("frame length out of bounds")
        if len(buf) < total:
            return None
        if zlib.crc32(buf[: total - 4]) != _CRC.unpack_from(buf, total - 4)[0]:
            raise EventStreamError("message CRC mismatch")
        headers = _headers(bytes(buf[_PRELUDE_LEN : _PRELUDE_LEN + header_len]))
        payload = bytes(buf[_PRELUDE_LEN + header_len : total - 4])
        del buf[:total]
        return _to_event(headers, payload)


def _headers(raw: bytes) -> Mapping[str, str]:
    out: dict[str, str] = {}
    pos = 0
    try:
        while pos < len(raw):
            name_len = raw[pos]
            name = raw[pos + 1 : pos + 1 + name_len].decode()
            kind = raw[pos + 1 + name_len]
            pos += 2 + name_len
            if kind in (_STRING, _BYTES):
                (size,) = struct.unpack_from(">H", raw, pos)
                value = raw[pos + 2 : pos + 2 + size]
                pos += 2 + size
                if kind == _STRING:
                    out[name] = value.decode()
            elif kind in _FIXED_SIZE:
                pos += _FIXED_SIZE[kind]
            else:
                raise EventStreamError("unknown header value type")
    except (struct.error, IndexError, UnicodeDecodeError) as exc:
        raise EventStreamError("malformed frame headers") from exc
    if pos != len(raw):
        raise EventStreamError("malformed frame headers")
    return out


def _to_event(headers: Mapping[str, str], payload: bytes) -> SseEvent:
    kind = headers.get(":message-type")
    if kind == "event" and ":event-type" in headers:
        name = headers[":event-type"]
    elif kind == "exception":
        name = "exception:" + headers.get(":exception-type", "unknown")
    else:
        raise EventStreamError("frame is neither an event nor an exception")
    try:
        return SseEvent(name, payload.decode())
    except UnicodeDecodeError as exc:
        raise EventStreamError("payload is not UTF-8") from exc


class UsageCollector:
    """Reads usage from the raw decoded events: the final `metadata` event carries it."""

    def __init__(self) -> None:
        self._usage: Usage | None = None

    def feed(self, event: SseEvent) -> None:
        if event.event != "metadata":
            return
        usage = converse_usage(as_obj(load_json(event.data)).get("usage"))
        if usage is not None:
            self._usage = usage

    def result(self) -> Usage | None:
        return self._usage


class ChunkTranslator:
    """Converse stream events -> OpenAI `chat.completion.chunk` SSE events, then `[DONE]`."""

    def __init__(
        self, model: str, response_id: str, created: int, *, include_usage: bool = True
    ) -> None:
        self._include_usage = include_usage  # the client asked for the usage-only chunk
        self._base: dict[str, Json] = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
        }
        self._tools: dict[int, int] = {}  # Converse block index -> OpenAI tool_calls index
        self.done = False

    def convert(self, event: SseEvent) -> list[SseEvent]:
        name = event.event or ""
        if name.startswith("exception:"):
            return self._error(name)
        parsed = load_json(event.data)
        if parsed is None:
            raise EventStreamError("event payload is not JSON")
        body = as_obj(parsed)
        handlers = {
            "messageStart": lambda: [self._delta({"role": "assistant", "content": ""})],
            "contentBlockStart": lambda: self._block_start(body),
            "contentBlockDelta": lambda: self._block_delta(body),
            "messageStop": lambda: [
                self._chunk([self._choice({}, finish_reason(body.get("stopReason")))])
            ],
            "metadata": lambda: self._metadata(body),
        }
        handler = handlers.get(name)  # contentBlockStop and unknown events carry nothing for chat
        return handler() if handler else []

    def _metadata(self, body: Mapping[str, Json]) -> list[SseEvent]:
        usage = converse_usage(body.get("usage"))
        # The usage-only chunk has empty `choices`; the client sees it only if it asked.
        tail = [self._chunk([], openai_usage(usage))] if usage and self._include_usage else []
        return [*tail, *self.finish()]

    def finish(self) -> list[SseEvent]:
        if self.done:
            return []
        self.done = True
        return [SseEvent(None, "[DONE]")]

    def _chunk(self, choices: list[Json], usage: Json = None) -> SseEvent:
        body: dict[str, Json] = {**self._base, "choices": choices}
        if usage is not None:
            body["usage"] = usage
        return SseEvent(None, dumps(body))

    @staticmethod
    def _choice(delta: Mapping[str, Json], finish: str | None = None) -> Json:
        return {"index": 0, "delta": delta, "finish_reason": finish}

    def _delta(self, delta: Mapping[str, Json]) -> SseEvent:
        return self._chunk([self._choice(delta)])

    def _block_start(self, body: Mapping[str, Json]) -> list[SseEvent]:
        tool = as_obj(as_obj(body.get("start")).get("toolUse"))
        index = body.get("contentBlockIndex")
        if not tool or not isinstance(index, int):
            return []
        slot = self._tools.setdefault(index, len(self._tools))
        call: Json = {
            "index": slot,
            "id": tool.get("toolUseId"),
            "type": "function",
            "function": {"name": tool.get("name"), "arguments": ""},
        }
        return [self._delta({"tool_calls": [call]})]

    def _block_delta(self, body: Mapping[str, Json]) -> list[SseEvent]:
        delta = as_obj(body.get("delta"))
        text = delta.get("text")
        if isinstance(text, str):
            return [self._delta({"content": text})]
        fragment = as_obj(delta.get("toolUse")).get("input")
        index = body.get("contentBlockIndex")
        if isinstance(fragment, str) and isinstance(index, int) and index in self._tools:
            call: Json = {"index": self._tools[index], "function": {"arguments": fragment}}
            return [self._delta({"tool_calls": [call]})]
        return []  # reasoning content is not part of the chat shape

    def _error(self, name: str) -> list[SseEvent]:
        throttled = "throttling" in name.lower()
        err: Json = {
            "error": {
                "type": "rate_limit_error" if throttled else "upstream_error",
                "code": "upstream_throttled" if throttled else "upstream_unavailable",
                "message": "The upstream stream failed.",
                "param": None,
            }
        }
        self.done = True
        return [SseEvent(None, json.dumps(err, separators=(",", ":"))), SseEvent(None, "[DONE]")]


class BedrockStream:
    """One ConverseStream answer: raw bytes in, OpenAI chat SSE bytes out, usage on the side."""

    def __init__(
        self,
        model: str,
        response_id: str,
        created: int,
        *,
        include_usage: bool = True,
        limits: SseLimits = SseLimits(),  # noqa: B008 - frozen default
    ) -> None:
        self._meter = StreamMeter(limits)
        self._decoder = EventStreamDecoder(limits.max_event_bytes)
        self._usage = UsageCollector()
        self._chunks = ChunkTranslator(model, response_id, created, include_usage=include_usage)

    def feed(self, chunk: bytes) -> bytes:
        self._meter.add_bytes(len(chunk))
        out: list[SseEvent] = []
        for event in self._decoder.feed(chunk):
            self._meter.add_event()
            self._usage.feed(event)
            try:
                out += self._chunks.convert(event)
            except UNREADABLE as exc:  # a hostile payload is a broken stream, not a crash
                raise EventStreamError("event payload is not usable") from exc
        return b"".join(serialise(e) for e in out)

    def close(self) -> bytes:
        """Call at the end of the body: rejects a half frame, emits `[DONE]` if still owed."""
        self._decoder.close()
        return b"".join(serialise(e) for e in self._chunks.finish())

    def usage(self) -> Usage | None:
        return self._usage.result()
