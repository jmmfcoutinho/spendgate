"""OpenTelemetry tracing for the gateway: the pipeline's `Tracer` port over the `otel` extra.

Imported only when `observability.otel.enabled` is true and the extra is installed (`cli.serve`
checks both), so the base install never loads OpenTelemetry. The exporter is OTLP over HTTP and
reads the standard `OTEL_EXPORTER_OTLP_*` variables; the sampler follows `OTEL_TRACES_SAMPLER`.
The tracer provider is local to this process's gateway: nothing global is set, so an application
that embeds spendgate keeps its own.

What leaves the process is an allowlist, as for logs (`gateway.logs`): the attribute names in
`ALLOWED_ATTRIBUTES` with scalar values, never a string shaped like a key. Exceptions are not
recorded as span events (their messages may quote a body or a secret); only `error.type`, the
class name, is set. Only `traceparent` crosses in either direction: `tracestate` and baggage are
caller-made values and are neither read nor passed on to the provider.
"""

import contextlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

import httpx
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from spendgate import package_version
from spendgate.gateway.logs import secret_shaped
from spendgate.pipeline import tracing
from spendgate.pipeline.model import Sender

TRACEPARENT = "traceparent"
# The only attributes a spendgate span may carry. Adding one is a reviewed change.
ALLOWED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "spendgate.request_id", "spendgate.model", "spendgate.endpoint", "spendgate.deployment",
        "spendgate.outcome", "spendgate.attempts", "spendgate.released", "spendgate.booked",
        "error.type",
    }
)  # fmt: skip
_MAX_TEXT = 256
_KINDS = {tracing.FORWARD: SpanKind.CLIENT}
_PROPAGATOR = TraceContextTextMapPropagator()


def allowed(attributes: Mapping[str, object]) -> dict[str, str | int | bool]:
    """Allowlisted names with scalar, short, secret-free values; everything else is dropped."""
    kept: dict[str, str | int | bool] = {}
    for name, value in attributes.items():
        if name not in ALLOWED_ATTRIBUTES or not isinstance(value, str | int | bool):
            continue
        if isinstance(value, str) and (len(value) > _MAX_TEXT or secret_shaped(value)):
            continue
        kept[name] = value
    return kept


class _Trace:
    """One call's parent: the caller's context, or (none sent) the first span of the call."""

    def __init__(self, context: Context | None) -> None:
        self.context = context


class _Handle:
    def __init__(self, span: trace.Span) -> None:
        self._span = span

    def set(self, name: str, value: tracing.Attribute) -> None:
        kept = allowed({name: value})
        if kept:
            with contextlib.suppress(Exception):
                self._span.set_attributes(kept)

    def failed(self, exc: BaseException) -> None:
        with contextlib.suppress(Exception):
            self._span.set_attribute("error.type", type(exc).__name__)
            self._span.set_status(Status(StatusCode.ERROR))  # no description: it is free text


class _NoSpan:
    def set(self, name: str, value: tracing.Attribute) -> None:
        pass


class OtelTracer:
    """`pipeline.tracing.Tracer` over an OpenTelemetry tracer. Never raises into the pipeline."""

    def __init__(self, tracer: trace.Tracer) -> None:
        self._tracer = tracer

    def parent(self, headers: Mapping[str, str]) -> object:
        """The caller's `traceparent` (tracestate and baggage ignored); invalid means none."""
        value = headers.get(TRACEPARENT)
        found: Context | None = None
        if value:
            with contextlib.suppress(Exception):
                extracted = _PROPAGATOR.extract({TRACEPARENT: value})
                if trace.get_current_span(extracted).get_span_context().is_valid:
                    found = extracted
        return _Trace(found)

    @contextlib.contextmanager
    def span(
        self, name: str, parent: object, attributes: Mapping[str, tracing.Attribute]
    ) -> Iterator[tracing.Span]:
        """Start the span as current, so the upstream request in it carries its traceparent."""
        holder = parent if isinstance(parent, _Trace) else _Trace(None)
        started: trace.Span | None = None
        try:
            # An empty context, not the current one: an untraced call starts its own trace.
            base = holder.context if holder.context is not None else Context()
            started = self._tracer.start_span(
                name, context=base, kind=_KINDS.get(name, SpanKind.INTERNAL),
                attributes=allowed(attributes), record_exception=False,
                set_status_on_exception=False,
            )  # fmt: skip
            current = trace.set_span_in_context(started, base)
            if holder.context is None:
                holder.context = current  # later spans of this call become its children
            token = otel_context.attach(current)
        except Exception:
            started = None
        if started is None:  # outside the handler: the step's error must not chain to this one
            yield _NoSpan()
            return
        handle = _Handle(started)
        try:
            yield handle
        except BaseException as exc:
            handle.failed(exc)
            raise
        finally:
            with contextlib.suppress(Exception):
                otel_context.detach(token)
            with contextlib.suppress(Exception):
                started.end()


class TracingSender:
    """Adds the current span's `traceparent` to each upstream request, and nothing else."""

    def __init__(self, inner: Sender) -> None:
        self._inner = inner

    async def send(self, request: httpx.Request, *, stream: bool = False) -> httpx.Response:
        carrier: dict[str, str] = {}
        with contextlib.suppress(Exception):
            _PROPAGATOR.inject(carrier)
        if value := carrier.get(TRACEPARENT):
            request.headers[TRACEPARENT] = value
        return await self._inner.send(request, stream=stream)


@dataclass(frozen=True, slots=True)
class Tracing:
    tracer: OtelTracer
    provider: TracerProvider

    def shutdown(self) -> None:
        """Flush what is queued and stop the exporter (at the end of `serve`)."""
        with contextlib.suppress(Exception):
            self.provider.shutdown()


def start(service_name: str, processor: SpanProcessor | None = None) -> Tracing:
    """A tracer provider for this gateway; OTLP/HTTP in batches unless a processor is given."""
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(processor or BatchSpanProcessor(_exporter()))
    return Tracing(OtelTracer(provider.get_tracer("spendgate", package_version())), provider)


def _exporter() -> SpanExporter:
    # Imported here: the exporter module reads OTEL_* settings and pulls in protobuf and requests.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
        OTLPSpanExporter,
    )

    return OTLPSpanExporter()
