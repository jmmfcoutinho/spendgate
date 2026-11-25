"""The tracing port: one span per hold, forward and settle, off unless the gateway plugs one in.

The pipeline names the spans and the facts on them; it never imports a tracing library. The
gateway's OpenTelemetry adapter (`gateway.tracing`, the `otel` extra) implements `Tracer`, and
`NoTracer` is the default, so library mode and an install without the extra pay nothing.

Span attributes are an allowlist enforced by the adapter: request id, catalog model, deployment
label, endpoint path, outcome and counts. Never a prompt, a key, a user, tag or session label.
"""

import contextlib
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import Protocol

type Attribute = str | int | bool

HOLD = "spendgate.hold"
FORWARD = "spendgate.forward"
SETTLE = "spendgate.settle"


class Span(Protocol):
    def set(self, name: str, value: Attribute) -> None:
        """Add one attribute; a name off the adapter's allowlist is dropped."""
        ...


class Tracer(Protocol):
    """Must never raise: a tracing fault may not change how a call is held, sent or settled."""

    def parent(self, headers: Mapping[str, str]) -> object:
        """The caller's trace context from `traceparent` (opaque to the pipeline), or None."""
        ...

    def span(
        self, name: str, parent: object, attributes: Mapping[str, Attribute]
    ) -> AbstractContextManager[Span]:
        """A span around one step, current while it runs (so the upstream call can carry it)."""
        ...


class _NoSpan:
    def set(self, name: str, value: Attribute) -> None:
        pass


_NO_SPAN = _NoSpan()


class NoTracer:
    def parent(self, headers: Mapping[str, str]) -> object:
        return None

    def span(
        self, name: str, parent: object, attributes: Mapping[str, Attribute]
    ) -> AbstractContextManager[Span]:
        return nullcontext(_NO_SPAN)


class _GuardedSpan:
    def __init__(self, inner: Span) -> None:
        self._inner = inner

    def set(self, name: str, value: Attribute) -> None:
        with contextlib.suppress(Exception):
            self._inner.set(name, value)


class Guarded:
    """Holds a plugged-in tracer to its promise: whatever it raises is dropped, and it can never
    swallow the step's own exception. A hold span that failed on exit would otherwise turn a held
    call into a 500 with a hold nobody owns, which the sweeper charges in full."""

    def __init__(self, inner: Tracer) -> None:
        self._inner = inner

    def parent(self, headers: Mapping[str, str]) -> object:
        try:
            return self._inner.parent(headers)
        except Exception:
            return None

    @contextlib.contextmanager
    def span(
        self, name: str, parent: object, attributes: Mapping[str, Attribute]
    ) -> Iterator[Span]:
        started = False
        try:
            manager = self._inner.span(name, parent, attributes)
            inner = manager.__enter__()
            started = True
        except Exception:  # noqa: S110 - the step runs untraced, outside this handler (below)
            pass
        if not started:  # yielding inside the handler would chain the tracer's error to the step's
            yield _NO_SPAN
            return
        try:
            yield _GuardedSpan(inner)
        except BaseException as exc:
            with contextlib.suppress(Exception):  # its return value is ignored: never suppressed
                manager.__exit__(type(exc), exc, exc.__traceback__)
            raise
        with contextlib.suppress(Exception):
            manager.__exit__(None, None, None)


def tags(request_id: str, model: str, endpoint: str) -> dict[str, Attribute]:
    """What every spendgate span carries: ids made by spendgate or config, never by the caller."""
    return {
        "spendgate.request_id": request_id,
        "spendgate.model": model,
        "spendgate.endpoint": endpoint,
    }
