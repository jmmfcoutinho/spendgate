"""Session context and attribution headers, shared by proxy mode (6.1) and library mode (6.2).

Why a contextvar plus an httpx request hook (not `default_headers` per call): SDK default
headers are fixed when the client is built, while a session scope is entered later, around
arbitrary calls. A contextvar is per thread and per asyncio task (tasks copy the context at
creation, so `asyncio.gather` children see the session they were started under), and an httpx
`request` hook reads it at send time for sync and async clients alike.
"""

import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol, cast

SESSION_HEADER = "X-Spendgate-Session"
USER_HEADER = "X-Spendgate-User"
TAGS_HEADER = "X-Spendgate-Tags"

_LABEL = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_TAG = re.compile(r"[a-z0-9._:-]{1,32}")
MAX_TAGS = 10
LIMIT_ARGS = ("budget", "max_steps", "max_seconds", "idle_seconds")


@dataclass(frozen=True, slots=True)
class SessionContext:
    """The active session: its id and, in library mode only, its limits."""

    id: str
    budget: str | None = None
    max_steps: int | None = None
    max_seconds: float | None = None
    idle_seconds: float | None = None

    @property
    def has_limits(self) -> bool:
        return any(getattr(self, name) is not None for name in LIMIT_ARGS)


_current: ContextVar[SessionContext | None] = ContextVar("spendgate_session", default=None)


def current_session() -> SessionContext | None:
    return _current.get()


def check_label(value: str, header: str) -> str:
    """Refuse a value the gateway would refuse (never silently fix it); blocks CR/LF too."""
    if not isinstance(value, str) or _LABEL.fullmatch(value) is None:
        raise ValueError(f"{header}: expected 1-128 characters from [A-Za-z0-9._:-], got {value!r}")
    return value


def tags_header(tags: Iterable[str]) -> str | None:
    items = [tags] if isinstance(tags, str) else list(tags)
    for tag in items:
        if not isinstance(tag, str) or _TAG.fullmatch(tag) is None:
            raise ValueError(
                f"{TAGS_HEADER}: each tag must match [a-z0-9._:-]{{1,32}}, got {tag!r}"
            )
    if len(items) > MAX_TAGS:
        raise ValueError(f"{TAGS_HEADER}: at most {MAX_TAGS} tags, got {len(items)}")
    return ",".join(items) or None


def attribution_headers(
    session: str | None, user: str | None, tags: Iterable[str]
) -> dict[str, str]:
    """Static headers for a client; a session set by `session()` overrides `session` per call."""
    headers: dict[str, str] = {}
    if session is not None:
        headers[SESSION_HEADER] = check_label(session, SESSION_HEADER)
    if user is not None:
        headers[USER_HEADER] = check_label(user, USER_HEADER)
    joined = tags_header(tags)
    if joined is not None:
        headers[TAGS_HEADER] = joined
    return headers


@contextmanager
def activate_session(ctx: SessionContext) -> Iterator[SessionContext]:
    """Make `ctx` current for the block; nesting restores the outer one (library mode reuses)."""
    check_label(ctx.id, SESSION_HEADER)
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


@contextmanager
def session(
    session_id: str,
    budget: str | None = None,
    max_steps: int | None = None,
    max_seconds: float | None = None,
    idle_seconds: float | None = None,
) -> Iterator[SessionContext]:
    """Tag every call in the block with `session_id` (proxy mode).

    Proxy mode takes its limits from the key's `session_defaults`; a limit argument here
    would be silently ignored, so it raises instead.
    """
    ctx = SessionContext(session_id, budget, max_steps, max_seconds, idle_seconds)
    if ctx.has_limits:
        raise ValueError(
            "session limits (budget, max_steps, max_seconds, idle_seconds) come from the key's "
            "session_defaults in proxy mode; set them in the gateway config"
        )
    with activate_session(ctx) as active:
        yield active


class _Headers(Protocol):
    def __setitem__(self, key: str, value: str) -> None: ...


class _Request(Protocol):
    @property
    def headers(self) -> _Headers: ...


class _HttpClient(Protocol):
    event_hooks: dict[str, list[object]]  # list invariance: hooks are stored as plain objects


def _stamp(request: _Request) -> None:
    ctx = _current.get()
    if ctx is None:
        return
    if ctx.has_limits:  # defensive: a library-mode context leaked into a proxy client
        raise ValueError("session limits are not supported by a proxy-mode client")
    request.headers[SESSION_HEADER] = ctx.id


def _stamp_sync(request: _Request) -> None:
    _stamp(request)


async def _stamp_async(request: _Request) -> None:
    _stamp(request)


def attach_session_hook(http_client: object) -> None:
    """Add the session request hook to an httpx-style client (ours or the caller's).

    Duck-typed on purpose: the OpenAI SDK uses `httpx`, Anthropic SDK 2.x uses `httpx2`, and an
    `isinstance` check against either would refuse the other. Async clients are the ones with
    `aclose`, and need a coroutine hook.
    """
    client = cast("_HttpClient", http_client)
    hooks = dict(client.event_hooks)
    hook = _stamp_async if hasattr(client, "aclose") else _stamp_sync
    hooks["request"] = [*hooks.get("request", []), hook]
    client.event_hooks = hooks
