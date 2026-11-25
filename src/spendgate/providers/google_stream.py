"""Gemini `alt=sse` stream to OpenAI `chat.completion.chunk` SSE: the Google `StreamRelay`.

Kept apart from `google.py` for size. It imports only `core`, `providers.port`, `sse` and
`google_shapes`.
"""

import json

from spendgate.core.cost import Usage
from spendgate.providers.google_shapes import (
    candidate,
    chat_usage,
    finishes,
    has_billed_output,
    has_output,
    has_thinking,
    load,
    read_usage,
    shrinks,
)
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
        self._meta: Json = None  # the latest `usageMetadata`; final only once `_final` is set
        self._output_seen = False  # billed content so far: then a candidates count is required
        self._thinking_seen = False  # a thought or a signature so far: a thoughts count is needed
        self._finished = False  # a terminal finish reason (or a blocked prompt) arrived
        self._final = False  # `_meta` came from that chunk or a later one
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
        text, calls, finish = candidate(chunk)
        ends = finishes(chunk)
        if has_output(chunk) and not ends:
            self._finished = False  # content after a finish: the answer was not over after all
        self._finished = self._finished or ends
        # Every chunk carries the running totals; only those sent with or after the finish reason
        # are the answer's final usage. A stream cut before then has none (charge the hold, M-13).
        # They are judged at the end, against all content seen: a missing candidates count is no
        # zero for an answer that had content.
        if "usageMetadata" in chunk:
            if shrinks(self._meta, chunk["usageMetadata"]):
                self._unreadable = True  # cumulative totals that drop cannot be the final ones
            self._meta = chunk["usageMetadata"]
        self._output_seen = self._output_seen or has_billed_output(chunk)
        self._thinking_seen = self._thinking_seen or has_thinking(chunk)
        self._final = self._finished and ("usageMetadata" in chunk or self._final)
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

    def _usage(self) -> Usage | None:
        """The final totals, read against all content seen; None before the finish reason."""
        if not self._final:
            return None
        return read_usage(
            self._meta, output_seen=self._output_seen, thinking_seen=self._thinking_seen
        )

    def usage(self) -> Usage | None:
        """Final usage only: a running total from before the finish reason is never trusted."""
        return None if self._unreadable else bounded(self._usage())

    def close(self) -> list[SseEvent]:
        """The usage-only chunk (empty choices; only if the client asked) and `[DONE]`."""
        frames = []
        usage = self.usage()  # the client sees what is charged: none once unreadable
        if usage is not None and self._include_usage:
            frames.append(self._event([], chat_usage(usage)))
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
