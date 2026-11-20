"""The small interface each provider shape implements for the fake upstream."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from tests.fakes.script import Script

JSON = "application/json"
SSE = "text/event-stream"


@dataclass(frozen=True, slots=True)
class Ctx:
    """Everything a shape needs to answer one request."""

    script: Script
    body: Mapping[str, object]  # parsed JSON request body ({} for multipart or empty bodies)
    path: str
    query: Mapping[str, str]

    @property
    def usage(self) -> object:
        return self.script.usage


@dataclass(frozen=True, slots=True)
class Reply:
    content_type: str
    body: bytes
    headers: tuple[tuple[str, str], ...] = ()


class Shape(Protocol):
    name: str

    def matches(self, path: str) -> bool: ...

    def streaming(self, ctx: Ctx) -> bool:
        """Whether this request asks for a stream (body flag or streaming path)."""
        ...

    def reply(self, ctx: Ctx) -> Reply:
        """The non-streamed success answer."""
        ...

    def stream(self, ctx: Ctx) -> tuple[str, list[bytes]]:
        """Content type and wire frames (one per event) of the streamed success answer."""
        ...

    def error(self, status: int, message: str) -> Reply:
        """The shape's error body."""
        ...


def dumps(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload).encode()
