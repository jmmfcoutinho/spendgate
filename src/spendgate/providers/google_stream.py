"""Gemini `alt=sse` stream to OpenAI `chat.completion.chunk` SSE: the Google `StreamRelay`.

Kept apart from `google.py` for size. It imports only `core`, `providers.port`, `sse` and
`google_shapes`.
"""

import json

from spendgate.core.cost import Usage
from spendgate.providers.google_shapes import candidate, chat_usage, load, read_usage
from spendgate.providers.port import UNREADABLE, Json, SseEvent, SseLimits, bounded
from spendgate.providers.sse import SseParser, serialise


class ChatStream:
    """Gemini events to OpenAI chat chunk events; one per stream."""

    def __init__(
        self, model: str, response_id: str, created: int, *, include_usage: bool = True
    ) -> None:
        self._model, self._id, self._created = model, response_id, created
        self._include_usage = include_usage  # the client asked for the usage-only chunk
        self._started, self._calls = False, 0
        self._usage: Usage | None = None
        self._unreadable = False  # an event we could not convert: usage is no longer trusted

    def feed(self, event: SseEvent) -> list[SseEvent]:
        try:
            return self._convert(event)
        except UNREADABLE:  # hostile payload (deep nesting, wrong types): drop it, charge the hold
            self._unreadable = True
            return []

    def _convert(self, event: SseEvent) -> list[SseEvent]:
        chunk = load(event.data)
        if chunk is None:
            return []
        # Every chunk repeats the running totals, so the last one carrying them is final.
        if "usageMetadata" in chunk:
            self._usage = read_usage(chunk["usageMetadata"])
        text, calls, finish = candidate(chunk)
        delta: dict[str, Json] = {} if self._started else {"role": "assistant"}
        self._started = True
        if text:
            delta["content"] = text
        if calls:
            delta["tool_calls"] = [{**c, "index": self._calls + i} for i, c in enumerate(calls)]
            self._calls += len(calls)
        if not delta and finish is None:
            return []
        return [self._event([{"index": 0, "delta": delta, "finish_reason": finish}])]

    def usage(self) -> Usage | None:
        return None if self._unreadable else bounded(self._usage)

    def close(self) -> list[SseEvent]:
        """The usage-only chunk (empty choices; only if the client asked) and `[DONE]`."""
        frames = []
        if self._usage is not None and self._include_usage:
            frames.append(self._event([], chat_usage(self._usage)))
        return [*frames, SseEvent(None, "[DONE]")]

    def _event(self, choices: list[Json], usage: Json = None) -> SseEvent:
        frame: dict[str, Json] = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self._model,
            "choices": choices,
        }
        return SseEvent(None, json.dumps(frame | ({} if usage is None else {"usage": usage})))


class GoogleRelay:
    """Parses Gemini SSE bytes (with limits), converts via `ChatStream`, writes chat SSE bytes."""

    def __init__(self, stream: ChatStream, limits: SseLimits) -> None:
        self._parser = SseParser(limits)
        self._stream = stream

    def feed(self, chunk: bytes) -> bytes:
        events = (out for e in self._parser.feed(chunk) for out in self._stream.feed(e))
        return b"".join(serialise(e) for e in events)

    def close(self) -> bytes:
        events = [out for e in self._parser.close() for out in self._stream.feed(e)]
        return b"".join(serialise(e) for e in [*events, *self._stream.close()])

    def usage(self) -> Usage | None:
        return self._stream.usage()
