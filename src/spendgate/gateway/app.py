"""The HTTP adapter: a thin Starlette app over `Pipeline`. Every decision lives in the pipeline.

The POST routes come from `pipeline.model.ENDPOINTS`, so the gateway and library mode serve the
same endpoints. Observability endpoints come from `gateway.observability`; the admin API is in
`gateway.admin`.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from spendgate.gateway.metrics import Metrics
from spendgate.gateway.observability import (
    ReadinessChecks,
    livez_endpoint,
    metrics_endpoint,
    readyz_endpoint,
)
from spendgate.pipeline.intake import ClientGone
from spendgate.pipeline.model import ENDPOINTS, ByteStream, Endpoint, Reply
from spendgate.pipeline.pipeline import Pipeline

_ANY = ["GET", "POST", "PUT", "PATCH", "DELETE"]
type View = Callable[[Request], Awaitable[Response]]


class RelayResponse(Response):
    """A live stream as an ASGI response that knows when the client is gone.

    Starlette's own streaming response notices a vanished client only when a send fails (or via a
    task group on older ASGI servers). This one listens for `http.disconnect` itself and always
    closes the body, which aborts the upstream call and settles the hold exactly once (M-13).
    """

    def __init__(self, stream: ByteStream, status: int, headers: dict[str, str]) -> None:
        super().__init__(b"", status, headers)
        self._stream = stream
        # Starlette added `content-length: 0` for the empty body; a stream has no length.
        self.raw_headers = [(k, v) for k, v in self.raw_headers if k != b"content-length"]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        pump = asyncio.ensure_future(self._pump(send))
        gone = asyncio.ensure_future(_wait_for_disconnect(receive))
        try:
            await asyncio.wait({pump, gone}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (pump, gone):
                task.cancel()  # a client that left cancels the read: the body then settles itself
            outcome = await asyncio.gather(pump, gone, return_exceptions=True)
            await asyncio.shield(self._stream.aclose())  # idempotent: settles if nothing did
        failure = outcome[0]
        if isinstance(failure, Exception) and not isinstance(failure, OSError):
            raise failure  # a bug; a send that failed because the client left is not one

    async def _pump(self, send: Send) -> None:
        await send(
            {"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers}
        )
        async for chunk in self._stream:
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _wait_for_disconnect(receive: Receive) -> None:
    while (await receive())["type"] != "http.disconnect":
        pass


def to_response(reply: Reply) -> Response:
    """A pipeline `Reply` as a Starlette response (a live stream stays a stream)."""
    if reply.stream is not None:
        return RelayResponse(reply.stream, reply.status, dict(reply.headers))
    return Response(reply.body, reply.status, dict(reply.headers))


async def _body(request: Request) -> AsyncIterator[bytes]:
    """The request body; a client that leaves mid-upload becomes the pipeline's `ClientGone`."""
    try:
        async for chunk in request.stream():
            yield chunk
    except ClientDisconnect as exc:
        raise ClientGone from exc


def _post(pipeline: Pipeline, endpoint: Endpoint) -> View:
    async def view(request: Request) -> Response:
        return to_response(await pipeline.serve(endpoint, request.headers, _body(request)))

    return view


def _models(pipeline: Pipeline) -> View:
    async def view(request: Request) -> Response:
        wanted = request.path_params.get("model_id")
        return to_response(await pipeline.models(request.headers, wanted))

    return view


def _not_found(pipeline: Pipeline) -> View:
    async def view(request: Request) -> Response:
        return to_response(pipeline.not_found(request.url.path))

    return view


def _unrouted(pipeline: Pipeline) -> Callable[[Request, Exception], Response]:
    """A newline in a `/v1` URL matches no route (`.*` skips it): the same JSON 404 (FZ-02)."""

    def handler(request: Request, exc: Exception) -> Response:
        if request.url.path.startswith("/v1/"):
            return to_response(pipeline.not_found(request.url.path))
        return PlainTextResponse("Not Found", status_code=404)  # outside /v1: Starlette's default

    return handler


def create_app(
    pipeline: Pipeline,
    *,
    metrics: Metrics | None = None,
    readiness: ReadinessChecks | None = None,
    background: Callable[[], Awaitable[None]] | None = None,
) -> Starlette:
    """The data-plane app. `/metrics` has no guard yet (5.4 decides admin exposure).

    `background` is a long-running job (the ledger sweeper) started with the app and cancelled
    when it stops, next to the lease heartbeat.
    """

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        jobs = [asyncio.ensure_future(pipeline.heartbeat())]  # keeps this instance's leases alive
        if background is not None:
            jobs.append(asyncio.ensure_future(background()))
        try:
            yield
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await pipeline.aclose()

    # One POST route per data-plane endpoint: a new endpoint in `ENDPOINTS` is served here too.
    routes = [Route(e.path, _post(pipeline, e), methods=["POST"]) for e in ENDPOINTS.values()]
    routes += [
        Route("/v1/models", _models(pipeline), methods=["GET"]),
        Route("/v1/models/{model_id:path}", _models(pipeline), methods=["GET"]),
        Route("/livez", livez_endpoint(), methods=["GET"]),
    ]
    if readiness is not None:
        routes.append(Route("/readyz", readyz_endpoint(readiness), methods=["GET"]))
    if metrics is not None:
        routes.append(Route("/metrics", metrics_endpoint(metrics), methods=["GET"]))
    routes.append(Route("/v1/{path:path}", _not_found(pipeline), methods=_ANY))
    return Starlette(
        routes=routes, lifespan=lifespan, exception_handlers={404: _unrouted(pipeline)}
    )
