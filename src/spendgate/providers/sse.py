"""Provider-neutral SSE codec and the passthrough relay shared by the SSE adapters.

Not an adapter (like `openai_shapes`): it imports only `core` and `providers.port`. The parser is
incremental and enforces `SseLimits` while it reads, so a hostile stream stops before it is
buffered (threat-model C-22). Bedrock decodes its own binary framing but counts against the same
`StreamMeter` and writes its output with `serialise`.
"""

import re
from collections.abc import Callable, Mapping
from typing import Protocol

from spendgate.core.cost import Usage
from spendgate.providers.port import (
    UNREADABLE,
    Json,
    SseEvent,
    SseLimits,
    StreamBroken,
    StreamLimitExceeded,
    bounded,
)

# A lone "\r" at the very end may still be the first half of "\r\n", so it waits for more bytes.
_LINE_END = re.compile(rb"\r\n|\n|\r(?!\Z)")


class StreamMeter:
    """Counts bytes and events of one stream against `SseLimits`; raises when one is crossed."""

    def __init__(self, limits: SseLimits) -> None:
        self._limits = limits
        self._bytes = 0
        self._events = 0

    def add_bytes(self, count: int) -> None:
        self._bytes += count
        if self._bytes > self._limits.max_stream_bytes:
            raise StreamLimitExceeded("stream is larger than the limit")

    def add_event(self) -> None:
        self._events += 1
        if self._events > self._limits.max_events:
            raise StreamLimitExceeded("stream has more events than the limit")


class SseParser:
    """Bytes in any split to complete `SseEvent`s. Comments, `id:` and `retry:` are dropped."""

    def __init__(self, limits: SseLimits = SseLimits()) -> None:  # noqa: B008 - frozen default
        self._limits = limits
        self._meter = StreamMeter(limits)
        self._buf = bytearray()
        self._name: str | None = None
        self._data: list[str] = []
        self._event_bytes = 0

    def feed(self, chunk: bytes) -> list[SseEvent]:
        self._meter.add_bytes(len(chunk))
        self._buf += chunk
        events: list[SseEvent] = []
        pos = 0
        while match := _LINE_END.search(self._buf, pos):
            if match.start() - pos > self._limits.max_line_bytes:
                raise StreamLimitExceeded("an SSE line is longer than the limit")
            if (event := self._line(bytes(self._buf[pos : match.start()]))) is not None:
                events.append(event)
            pos = match.end()
        del self._buf[:pos]
        # A pending final CR is a terminator, not a line byte (split-invariant, FZ-04).
        pending = len(self._buf) - 1 if self._buf.endswith(b"\r") else len(self._buf)
        if pending > self._limits.max_line_bytes:  # a partial line already too long
            raise StreamLimitExceeded("an SSE line is longer than the limit")
        return events

    def close(self) -> list[SseEvent]:
        """End of stream: a half-received line or event means the upstream was cut."""
        events = []
        if self._buf == b"\r":  # a final bare CR is a complete (empty) line
            self._buf.clear()
            events = [e for e in [self._line(b"")] if e is not None]
        if self._buf or self._data:
            raise StreamBroken("stream ended inside an event")
        return events

    def _line(self, raw: bytes) -> SseEvent | None:
        try:
            line = raw.decode()
        except UnicodeDecodeError as exc:
            raise StreamBroken("SSE line is not UTF-8") from exc
        if not line:  # a blank line dispatches the event
            return self._dispatch()
        self._event_bytes += len(raw) + 1
        if self._event_bytes > self._limits.max_event_bytes:
            raise StreamLimitExceeded("an SSE event is larger than the limit")
        field, _, value = line.partition(":")
        if not field:  # ":comment" (keep-alives)
            return None
        value = value.removeprefix(" ")
        if field == "event":
            self._name = value
        elif field == "data":
            self._data.append(value)
        return None

    def _dispatch(self) -> SseEvent | None:
        event = SseEvent(self._name, "\n".join(self._data)) if self._data else None
        self._name, self._data, self._event_bytes = None, [], 0
        if event is not None:
            self._meter.add_event()
        return event


def serialise(event: SseEvent) -> bytes:
    """One event as wire bytes; multi-line data becomes several `data:` lines."""
    head = f"event: {event.event}\n" if event.event else ""
    body = "".join(f"data: {line}\n" for line in event.data.split("\n"))
    return f"{head}{body}\n".encode()


def asked_for_usage(body: Mapping[str, Json]) -> bool:
    """True when the client's chat body set `stream_options.include_usage` itself."""
    options = body.get("stream_options")
    return isinstance(options, Mapping) and options.get("include_usage") is True


class EventUsageReader(Protocol):
    """Fed every parsed event of one stream; yields the final usage."""

    def feed(self, event: SseEvent) -> None: ...

    def result(self) -> Usage | None: ...


class SseRelay:
    """Relays SSE events unchanged except those `hide` rejects; reads usage from every event."""

    def __init__(
        self,
        reader: EventUsageReader,
        limits: SseLimits,
        hide: Callable[[SseEvent], bool] | None = None,
    ) -> None:
        self._reader = reader
        self._parser = SseParser(limits)
        self._hide = hide
        self._unreadable = False  # a reader that choked on an event: its totals can't be trusted

    def feed(self, chunk: bytes) -> bytes:
        return self._emit(self._parser.feed(chunk))

    def close(self) -> bytes:
        return self._emit(self._parser.close())

    def _emit(self, events: list[SseEvent]) -> bytes:
        for event in events:
            try:
                self._reader.feed(event)
            except UNREADABLE:  # hostile payload: relay it, but charge the full hold
                self._unreadable = True
        return b"".join(serialise(e) for e in events if not (self._hide and self._hide(e)))

    def usage(self) -> Usage | None:
        if self._unreadable:
            return None
        try:
            return bounded(self._reader.result())
        except UNREADABLE:
            return None
