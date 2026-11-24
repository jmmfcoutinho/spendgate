"""The request pipeline: ten steps, one entry point, ports injected (no globals).

`Pipeline.serve` is what the HTTP adapter calls with raw request parts; `Pipeline.handle` takes an
already-parsed `Inbound` (library mode). A refusal is a `Refusal` value turned into a `Reply`; it
never reaches a provider. After the hold, `_forward_and_settle` guarantees exactly one money
transition (`release` or `settle`) even when the client cancels: the transition runs in a task
that a cancel cannot interrupt (T-MON-11, M-13). A streamed answer returns a live `StreamBody`
instead; it makes the same one transition when the stream ends (`pipeline.streaming`).
"""

import asyncio
import contextlib
import os
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace

from spendgate.config import Settings
from spendgate.core.errors import ErrorCode, Refusal, Shape
from spendgate.core.health import HealthConfig
from spendgate.core.limits import Admit, InMemoryLimitStore, LimitStore
from spendgate.core.money import Money
from spendgate.core.outcomes import Outcome
from spendgate.ledger.ids import new_id
from spendgate.ledger.key_types import Principal
from spendgate.ledger.port import ConflictError, Held, HoldRequest, LedgerPort, Refused
from spendgate.pipeline import answers, intake, replies, steps
from spendgate.pipeline.alerts import log_alerts
from spendgate.pipeline.auth import authenticate
from spendgate.pipeline.closing import Call, Closer, Sleeper
from spendgate.pipeline.faults import InternalEvent, fault_of
from spendgate.pipeline.leases import Leases
from spendgate.pipeline.model import (
    Clock,
    Endpoint,
    HeldEvent,
    IdGenerator,
    Inbound,
    KeyVerifier,
    NoopObserver,
    Observer,
    RefusedEvent,
    Reply,
    Sender,
    models_endpoint,
    report_internal,
)
from spendgate.pipeline.routing import Answered, Failed, Forwarder, Job, Router, unavailable
from spendgate.pipeline.settle import Book, Closed
from spendgate.pipeline.streaming import PING_SECONDS, STREAM_LIMITS, Live, StreamBody, StreamEnv
from spendgate.pipeline.upkeep import Upkeep
from spendgate.pipeline.watch import StreamFacts, session_ends_at
from spendgate.providers.port import Provider, SseLimits

_LEDGER_RETRY_SECONDS = 5
# How long a call waits for its hold. Above the Postgres lock and statement timeouts (2 s, 5 s),
# so those usually answer first; this catches the rest (pool wait, slow server, SQLite busy).
HOLD_SECONDS = 10.0
_MS = 1000
_LEDGER_DOWN = Refusal(
    ErrorCode.LEDGER_UNAVAILABLE,
    "The ledger is unavailable; try again.",
    retry_after_seconds=_LEDGER_RETRY_SECONDS,
)
_INTERNAL = answers.INTERNAL


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


class Pipeline:
    """Steps 1 to 10 over injected ports."""

    def __init__(  # noqa: PLR0913 - the ports are the point of the design
        self, *, settings: Settings, ledger: LedgerPort, keys: KeyVerifier,
        providers: Mapping[str, Provider], sender: Sender, limits: LimitStore | None = None,
        now_ms: Clock = system_ms, new_request_id: IdGenerator = request_id,
        observer: Observer | None = None, instance: str | None = None,
        gate: intake.Gate | None = None, stream_limits: SseLimits = STREAM_LIMITS,
        ping_seconds: float = PING_SECONDS, sleep: Sleeper = asyncio.sleep,
        max_request_seconds: float | None = None, body_timeout_seconds: float | None = None,
        hold_seconds: float = HOLD_SECONDS,
    ) -> None:  # fmt: skip
        missing = {r.catalog_id.partition("/")[0] for r in settings.routes.values()} - set(
            providers
        )
        if missing:  # a route nobody can serve is a start-up error, not a request-time surprise
            raise ValueError(f"no provider adapter for {sorted(missing)}")
        self.settings = settings
        server = settings.config.server
        biggest = max(server.max_body_bytes * intake.COPIES,
                      server.max_json_body_bytes * intake.JSON_AMPLIFICATION)  # fmt: skip
        self.gate = gate or intake.Gate(
            server.max_concurrent_requests, max(server.max_body_bytes, server.max_json_body_bytes),
            max(intake.DEFAULT_MEMORY_BYTES, biggest),
        )  # fmt: skip
        self._max_request_s = max_request_seconds or server.max_request_seconds
        self._body_timeout_s = body_timeout_seconds or server.body_read_timeout_seconds
        self._hold_s = hold_seconds
        self._ledger, self._keys, self._providers = ledger, keys, providers
        self._limits = limits or InMemoryLimitStore()
        self._now, self._new_id = now_ms, new_request_id
        self._observer = observer or NoopObserver()
        self._instance = instance or instance_id()
        health: HealthConfig = settings.health
        self._router = Router(settings.routes, health)
        self._forwarder = Forwarder(
            sender, self._router, settings.catalog, now_ms, self._observer,
            settings.config.routing.max_attempts, stream_limits,
        )  # fmt: skip
        self._closer = Closer(settings, ledger, self._limits, self._observer, now_ms, sleep)
        self._owned: set[str] = set()  # holds this pipeline is still responsible for closing
        self._leases = Leases(settings.lease_ms, settings.heartbeat_ms)
        self._upkeep = Upkeep(
            ledger, self._closer, self._leases, self._owned, now_ms, self._instance,
            settings.lease_ms,
        )  # fmt: skip
        self._ping_seconds = ping_seconds
        self._tasks: set[asyncio.Future[object]] = set()

    # --- step 1 and the adapter-facing entry points -------------------------------------------

    async def serve(
        self, endpoint: Endpoint, headers: Mapping[str, str], chunks: AsyncIterator[bytes]
    ) -> Reply:
        """Step 1, then everything else. Nothing is read from `chunks` before the key is known.

        Order (SR-01): key from the headers, the key's in-flight uploads, the gate, then the read.
        A caller without a valid key can therefore never hold a place or any memory.
        """
        rid = self._new_id()
        lowered = {k.lower(): v for k, v in headers.items()}
        who = await authenticate(self._keys, lowered, self._now())
        if isinstance(who, Refusal):
            return self._refuse(rid, endpoint, who)
        length = intake.content_length(lowered)
        if isinstance(length, Refusal):
            return self._refuse(rid, endpoint, length, who)
        taken = await steps.admit_intake(self._limits, who.policy, self.settings, self._now())
        if isinstance(taken, Refusal):
            return self._refuse(rid, endpoint, taken, who)
        try:
            return await self._admitted(rid, endpoint, lowered, length, chunks, who, taken)
        finally:  # idempotent: normally freed as soon as the body was read
            await self._closer.free_limits(taken)

    async def _admitted(  # noqa: PLR0913, PLR0917 - the facts serve already settled
        self, rid: str, endpoint: Endpoint, lowered: Mapping[str, str], length: int | None,
        chunks: AsyncIterator[bytes], who: Principal, taken: Admit | None,
    ) -> Reply:  # fmt: skip
        """Gate, read, parse, then the rest; the gate place is kept while the call runs."""
        server = self.settings.config.server
        cap = server.max_body_bytes if endpoint.multipart else server.max_json_body_bytes
        copies = intake.COPIES if endpoint.multipart else intake.JSON_AMPLIFICATION
        slot = self.gate.enter(length, max_body=cap, copies=copies, owner=who.key_id)
        if isinstance(slot, Refusal):
            return self._refuse(rid, endpoint, slot, who)
        try:
            try:
                raw = await intake.read_capped(chunks, cap, self._body_timeout_s, reserve=slot.grow)
            except intake.ClientGone:  # nobody to answer; nothing was held
                return self._refuse(
                    rid, endpoint, Refusal(ErrorCode.INVALID_REQUEST, "client left")
                )
            if isinstance(raw, Refusal):
                return self._refuse(rid, endpoint, raw, who, status=intake.status_of(raw))
            inbound = intake.parse_body(endpoint, lowered, raw)
            del raw  # the parsed body is the only copy kept while the upstream call runs
            await self._closer.free_limits(taken)  # the upload is over: real limits take over
            if isinstance(inbound, Refusal):
                status = intake.status_of(inbound)
                return self._refuse(rid, endpoint, inbound, who, status=status)
            return await self.handle(inbound, rid)
        finally:
            slot.release()

    async def handle(self, inbound: Inbound, rid: str | None = None) -> Reply:
        """Steps 2 to 10 for a parsed request."""
        rid = rid or self._new_id()
        endpoint = inbound.endpoint
        now = self._now()
        who = await authenticate(self._keys, inbound.headers, now)
        if isinstance(who, Refusal):
            return self._refuse(rid, endpoint, who)
        context = steps.Context(
            self.settings, self._providers, inbound, who.policy, rid, now, self._observer
        )
        prepared = steps.prepare(context)
        if isinstance(prepared, Refusal):
            return self._refuse(rid, endpoint, prepared, who, inbound)
        admit = await steps.admit_limits(
            self._limits, who.key_id, prepared.limits, prepared.tokens, now
        )
        if isinstance(admit, Refusal):
            return self._refuse(rid, endpoint, admit, who, inbound)
        call = Call(rid, inbound, who, prepared, admit, now)
        try:
            return await self._hold_and_forward(call)
        except Exception as exc:
            self._owned.discard(call.id)  # whatever is held is left to the sweeper, not renewed
            report_internal(self._observer, InternalEvent(rid, fault_of("pipeline.handle", exc)))
            await self._closer.free_limits(call.admit)
            return self._refuse(rid, endpoint, _INTERNAL, who, inbound)

    # --- step 7 -------------------------------------------------------------------------------

    async def _hold_and_forward(self, call: Call) -> Reply:
        prepared = call.prepared
        targets = self._router.targets(prepared.route.alias)
        request = steps.hold_request(
            self.settings, prepared, targets, call.principal.policy, call.inbound.endpoint.path,
            call.id, self._instance, call.started,
        )  # fmt: skip
        book = Book(prepared.pricing, targets[0].label)
        held = await self._hold(request, call, book)
        if not isinstance(held, Held):
            await self._closer.free_limits(call.admit)
            return self._refuse(call.id, call.inbound.endpoint, held, call.principal, call.inbound)
        self._owned.add(call.id)  # renewed by the heartbeat until its one transition is made
        book.budget_ids = frozenset(item.budget_id for item in held.items)
        log_alerts(held.alerts, self.settings.ledger_currency)  # soft-threshold crossings
        with contextlib.suppress(Exception):  # observers never affect money handling
            self._observer.on_held(
                HeldEvent(call.id, prepared.route.catalog_id, prepared.pricing.worst)
            )
        job = Job(
            call.id, prepared.request, prepared.provider, targets, prepared.free_statuses,
            prepared.route.catalog_id,
        )  # fmt: skip
        return await self._forward_and_settle(call, job, book, held)

    async def _hold(self, request: HoldRequest, call: Call, book: Book) -> Held | Refusal:
        """The admission transaction, shielded and bounded: neither a cancel nor the deadline may
        strand a committed hold."""
        task = asyncio.ensure_future(self._ledger.hold(request))
        deadline = asyncio.timeout(self._hold_s)
        try:
            async with deadline:
                result = await asyncio.shield(task)
        except asyncio.CancelledError:
            self._spawn(
                self._give_back(task, call, book)
            )  # before the upstream call: released at 0
            raise
        except ConflictError:
            return _INTERNAL
        except Exception:
            if deadline.expired():
                # Too slow: refuse now. The task runs on, bounded by the ledger's own timeouts,
                # and whatever it commits is released at 0; the caller frees the limits.
                self._spawn(self._give_back(task, replace(call, admit=None), book))
            else:  # the transaction may have committed: end it free, never swept at full
                await self._closer.release_unsent(call.id, book)
            return _LEDGER_DOWN
        return result.refusal if isinstance(result, Refused) else result

    async def _give_back(
        self, task: "asyncio.Future[Held | Refused]", call: Call, book: Book
    ) -> None:
        """A cancel or the deadline cut the hold short: if it may commit, release it (unsent)."""
        try:
            result = await task
        except Exception:
            result = None  # ambiguous: release is state-guarded, harmless if nothing committed
        if result is None or isinstance(result, Held):
            await self._closer.release_unsent(call.id, book)
        await self._closer.free_limits(call.admit)

    # --- step 8 and 9 -------------------------------------------------------------------------

    async def _forward_and_settle(self, call: Call, job: Job, book: Book, held: Held) -> Reply:
        result: Answered | Failed | Live | None = None
        standing: StreamFacts | None = None
        interrupted: Outcome | None = None
        pre: tuple[Money, str] | None = None
        try:
            if job.request.stream:  # headers go out before the cost exists: room after the hold
                pre = await self._closer.remaining(call.principal.scope_id, book.budget_ids)
                standing = await self._standing(call, held)
            async with asyncio.timeout(self._max_request_s):  # SR-13: no call runs forever
                result = await self._forwarder.run(job, book)
        except asyncio.CancelledError:
            interrupted = Outcome.CANCELLED  # after send: the full hold; before: released
            raise
        except TimeoutError:  # the deadline: sent means the full hold (TIMEOUT), else released
            interrupted = Outcome.TIMEOUT
            result = unavailable(job.targets[0].provider, "The request took too long.", 504)
        except Exception as exc:
            interrupted = Outcome.INTERNAL_ERROR
            result = Failed(_INTERNAL, 500, fault=fault_of("pipeline.forward", exc))
        finally:
            if not isinstance(result, Live):  # a live stream settles itself when it ends
                closing = self._spawn(self._close(call, book, interrupted))
                closed = await asyncio.shield(closing)  # a second cancel cannot stop the settle
        if isinstance(result, Live):
            return self._stream_reply(call, book, result, pre, standing)
        if isinstance(result, Failed) and result.fault is not None:  # after the close: charged?
            report_internal(self._observer, InternalEvent(call.id, result.fault, closed.charged))
        return answers.finished_reply(call, result, closed)

    async def _close(
        self, call: Call, book: Book, interrupted: Outcome | None, ttft_ms: int | None = None
    ) -> Closed:
        """The one transition; afterwards the hold is no longer ours to renew (booked or not)."""
        try:
            return await self._closer.close(call, book, interrupted, ttft_ms)
        finally:
            self._owned.discard(call.id)

    def _stream_reply(
        self, call: Call, book: Book, live: Live, pre: tuple[Money, str] | None,
        standing: StreamFacts | None,
    ) -> Reply:  # fmt: skip
        """Protocol 2.8: event stream headers at once; the cost comes later as an SSE comment."""
        env = StreamEnv(
            self.settings.catalog, self._now, self._observer, self._leases,
            lambda b, ttft: self._close(call, b, None, ttft),
            self._spawn, self._ping_seconds, int(self._max_request_s * _MS),
        )  # fmt: skip
        body = StreamBody(env, call.id, call.inbound.endpoint, book, live, call.started)
        if standing is not None:
            self._leases.watch(call.id, replace(standing, provider=live.provider))
        headers = replies.stream_headers(call.id, pre[0] if pre else None, pre[1] if pre else None)
        return Reply(200, headers, stream=body)

    async def _standing(self, call: Call, held: Held) -> StreamFacts | None:
        """What the heartbeat re-checks for this stream; None (not watched) if it cannot be read."""
        try:
            chain = await self._closer.chain_scope_ids(call.principal.scope_id)
            ends = await session_ends_at(self._ledger, held)
        except Exception:
            return None
        route = call.prepared.route
        return StreamFacts(call.principal.policy, chain, route.catalog_id, "", ends)

    # --- step 10: records, headers --------------------------------------------------------------

    def _refuse(  # noqa: PLR0913, PLR0917 - one refusal's independent facts
        self, rid: str, endpoint: Endpoint, refusal: Refusal, who: Principal | None = None,
        inbound: Inbound | None = None, status: int | None = None,
    ) -> Reply:  # fmt: skip
        """A refusal as a reply; nothing was held or sent. Cost header: zero, in ledger money."""
        model = steps.routed_model(self.settings.routes, inbound.body) if inbound else None
        event = RefusedEvent(rid, refusal.code, endpoint.path, model, who.key_id if who else None)
        with contextlib.suppress(Exception):
            self._observer.on_refused(event)
        zero = Money.zero(self.settings.ledger_currency)
        remaining, scope = replies.budget_details(refusal, self.settings.ledger_currency)
        return replies.error_reply(
            refusal,
            endpoint.shape,
            rid,
            status=status,
            charged=zero,
            remaining=remaining,
            scope=scope,
        )

    def not_found(self, path: str) -> Reply:
        """Any other path under /v1 (protocol 2)."""
        shape: Shape = "anthropic" if path.startswith("/v1/messages") else "openai"
        refusal = Refusal(ErrorCode.NOT_FOUND, "Unknown path.")
        return replies.error_reply(refusal, shape, self._new_id())

    # --- models list ----------------------------------------------------------------------------

    async def models(self, headers: Mapping[str, str], wanted: str | None = None) -> Reply:
        """`GET /v1/models[/{id}]`: priced aliases the calling key may use."""
        rid = self._new_id()
        lowered = {k.lower(): v for k, v in headers.items()}
        now = self._now()
        shape: Shape = "anthropic" if "anthropic-version" in lowered else "openai"
        who = await authenticate(self._keys, lowered, now)
        endpoint = models_endpoint(shape)
        if isinstance(who, Refusal):
            return self._refuse(rid, endpoint, who)
        listed = answers.listable(self.settings, who.policy, now)
        if wanted is not None:
            listed = [m for m in listed if m == wanted]
            if not listed:
                return self._refuse(
                    rid, endpoint, Refusal(ErrorCode.NOT_FOUND, "No such model."), who
                )
        return replies.models_reply(listed, shape, rid, wanted is not None)

    # --- read-only probes: what this instance still owes the ledger ----------------------------

    @property
    def instance(self) -> str:
        """The lease-owner name on this pipeline's holds (unique per process)."""
        return self._instance

    def owned(self) -> frozenset[str]:
        """Request ids whose hold this pipeline must still close; its heartbeat renews these."""
        return frozenset(self._owned)

    def unsettled(self) -> frozenset[str]:
        """Closes the ledger refused, waiting for a heartbeat retry or the sweeper."""
        return self._closer.unsettled()

    # --- background ----------------------------------------------------------------------------

    def _spawn[T](self, coro: Awaitable[T]) -> "asyncio.Future[T]":
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)  # type: ignore[arg-type]
        task.add_done_callback(self._tasks.discard)
        return task

    async def aclose(self) -> None:
        """Settle streams nobody closed, then wait for settlements that outlived their request."""
        await asyncio.gather(*(s.aclose() for s in self._leases.open()), return_exceptions=True)
        await self.drain()

    async def drain(self) -> None:
        """Wait for settlements that outlived their request; open streams are left running."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
            await asyncio.sleep(0)  # done tasks leave `_tasks` by callback; gather never yields

    async def heartbeat(self, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        """Renew this instance's leases forever (cancel to stop); a failed renewal is retried."""
        interval = self.settings.heartbeat_ms / _MS
        while True:
            await sleep(interval)
            await self.renew_leases()
            await self.watch_streams()

    async def watch_streams(self) -> None:
        """Stop open streams a kill switch or their session's time limit now forbids (2.8)."""
        await self._upkeep.watch_streams()

    async def renew_leases(self) -> None:
        """One heartbeat: renew owned leases; see `Upkeep.renew_leases`."""
        await self._upkeep.renew_leases()
