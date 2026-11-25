"""How a `Pipeline` is put together: its options, their defaults, and the parts built from them.

`Pipeline.__init__` declares the ports and knobs (`Knobs`) with their defaults and hands them all
to `assemble`, which checks that every route has an adapter, fills the knobs left as None and
builds each collaborator the pipeline owns, so `pipeline.pipeline` reads as the ten steps.
"""

import os
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Required, TypedDict

from spendgate.config import Settings
from spendgate.config.schema import Server
from spendgate.core.limits import InMemoryLimitStore, LimitStore
from spendgate.ledger.ids import new_id
from spendgate.ledger.port import MoneyLedger
from spendgate.pipeline import candidates, intake, tracing
from spendgate.pipeline.closing import Closer, Sleeper
from spendgate.pipeline.holding import Holder, Spawn
from spendgate.pipeline.leases import Leases
from spendgate.pipeline.model import Clock, IdGenerator, KeyVerifier, NoopObserver, Observer, Sender
from spendgate.pipeline.routing import Forwarder
from spendgate.pipeline.upkeep import Upkeep
from spendgate.providers.port import Provider, SseLimits

_MS = 1000


def system_ms() -> int:
    return time.time_ns() // 1_000_000


def instance_id() -> str:
    """A lease-owner name unique to this process: peers must never renew each other's holds.

    The load test driver (`tests/load/driver.py`, `owner_of`) finds a gateway's holds by the pid
    in this host-pid-tail form: keep the pid as the second-to-last `-` field or update it there.
    """
    return f"{socket.gethostname()}-{os.getpid()}-{new_id()[-6:]}"


def request_id() -> str:
    return "sg_req_" + new_id()


class Knobs(TypedDict, total=False):
    """The optional `Pipeline` keyword arguments; their defaults are in `Pipeline.__init__`.

    The one list of them: `gateway.wiring.PipelineKnobs` extends it rather than repeating it.
    """

    limits: LimitStore | None
    now_ms: Clock
    new_request_id: IdGenerator
    observer: Observer | None
    instance: str | None
    gate: intake.Gate | None
    stream_limits: SseLimits
    ping_seconds: float
    sleep: Sleeper
    max_request_seconds: float | None
    body_timeout_seconds: float | None
    hold_seconds: float
    tracer: tracing.Tracer | None


class PipelineOptions(Knobs):
    """Every `Pipeline` argument as `Pipeline.__init__` hands it to `assemble`: all keys present.

    A knob left as None takes the default `assemble` gives it: the server settings for the
    deadlines, an in-memory limit store, no observer and no tracer, a fresh instance name.
    """

    settings: Required[Settings]
    ledger: Required[MoneyLedger]
    keys: Required[KeyVerifier]
    providers: Required[Mapping[str, Provider]]
    sender: Required[Sender]


@dataclass(frozen=True, slots=True)
class Parts:
    """Everything one pipeline holds, built once when it is created."""

    settings: Settings
    ledger: MoneyLedger
    keys: KeyVerifier
    providers: Mapping[str, Provider]
    now: Clock
    new_id: IdGenerator
    gate: intake.Gate
    limits: LimitStore
    observer: Observer
    tracer: tracing.Tracer
    instance: str
    router: candidates.Router
    forwarder: Forwarder
    closer: Closer
    leases: Leases
    upkeep: Upkeep
    holder: Holder
    max_request_s: float
    body_timeout_s: float
    ping_seconds: float


def _default_gate(server: Server) -> intake.Gate:
    """One place per concurrent request, and memory for the largest body at its worst copy."""
    biggest = max(
        server.max_body_bytes * intake.COPIES,
        server.max_json_body_bytes * intake.JSON_AMPLIFICATION,
    )
    return intake.Gate(
        server.max_concurrent_requests,
        max(server.max_body_bytes, server.max_json_body_bytes),
        max(intake.DEFAULT_MEMORY_BYTES, biggest),
    )


def _forwarder(  # noqa: PLR0913, PLR0917 - the values `assemble` already resolved
    options: PipelineOptions,
    router: candidates.Router,
    now: Clock,
    sleep: Sleeper,
    observer: Observer,
    max_request_s: float,
) -> Forwarder:
    """The failover loop; a wait for a cooling deployment must fit inside the request deadline."""
    settings = options["settings"]
    return Forwarder(
        options["sender"],
        router,
        settings.catalog,
        now,
        observer,
        settings.config.routing.max_attempts,
        options["stream_limits"],
        sleep,
        max_request_s * _MS,
    )


def assemble(options: PipelineOptions, owned: set[str], spawn: Spawn) -> Parts:
    """Build a pipeline's parts; `owned` and `spawn` are the pipeline's own set and task spawner.

    Raises ValueError when a route names a provider no adapter serves.
    """
    settings, ledger, providers = options["settings"], options["ledger"], options["providers"]
    missing = {r.catalog_id.partition("/")[0] for r in settings.routes.values()} - set(providers)
    if missing:  # a route nobody can serve is a start-up error, not a request-time surprise
        raise ValueError(f"no provider adapter for {sorted(missing)}")
    server = settings.config.server
    gate = options["gate"] or _default_gate(server)
    now, sleep = options["now_ms"], options["sleep"]
    max_request_s = options["max_request_seconds"] or server.max_request_seconds
    limits = options["limits"] or InMemoryLimitStore()
    observer = options["observer"] or NoopObserver()
    traced = options["tracer"]
    tracer: tracing.Tracer = tracing.Guarded(traced) if traced else tracing.NoTracer()
    instance = options["instance"] or instance_id()
    router = candidates.Router(settings.routes, settings.health)
    forwarder = _forwarder(options, router, now, sleep, observer, max_request_s)
    closer = Closer(settings, ledger, limits, observer, now, sleep, tracer)
    leases = Leases(settings.lease_ms, settings.heartbeat_ms)
    upkeep = Upkeep(ledger, closer, leases, owned, now, instance, settings.lease_ms, observer)
    hold_seconds = options["hold_seconds"]
    return Parts(
        settings,
        ledger,
        options["keys"],
        providers,
        now,
        options["new_request_id"],
        gate,
        limits,
        observer,
        tracer,
        instance,
        router,
        forwarder,
        closer,
        leases,
        upkeep,
        Holder(ledger, closer, observer, tracer, now, spawn, hold_seconds),
        max_request_s,
        options["body_timeout_seconds"] or server.body_read_timeout_seconds,
        options["ping_seconds"],
    )
