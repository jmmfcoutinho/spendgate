"""One in-process pipeline: ledger, providers, upstream client and the maintenance task.

This is the library-mode twin of the gateway's composition root. It builds the same `Pipeline`
from the same `Settings`; only the front end (an httpx transport instead of Starlette) and the
verifier (`LocalPrincipal` instead of a key store) differ, so policy decisions cannot drift.
"""

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import httpx

from spendgate.client.session import SESSION_HEADER, SessionContext, activate_session
from spendgate.config import Settings
from spendgate.core.money import Money
from spendgate.ledger.ids import new_id
from spendgate.ledger.sqlite import SqliteLedger
from spendgate.local.principal import LocalPrincipal
from spendgate.local.setup import prepare_directory, prepare_scope
from spendgate.pipeline.adapters import build_providers
from spendgate.pipeline.model import ENDPOINTS, Clock, Reply
from spendgate.pipeline.pipeline import Pipeline, system_ms
from spendgate.pipeline.transport import upstream_timeout

PEPPER_VAR = "SPENDGATE_LOCAL_PEPPER"  # config files must name a pepper; library mode never uses it
_PLACEHOLDER_KEY = "Bearer spendgate-local"  # not a secret: the principal needs no credential
_CREDENTIALS = frozenset({"authorization", "x-api-key"})
_MODELS = "/v1/models"
_log = logging.getLogger("spendgate.local")


def local_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment `config.load` resolves `env:` references in."""
    return {**os.environ, PEPPER_VAR: "unused-in-library-mode", **(extra or {})}


@dataclass(frozen=True, slots=True)
class Call:
    """One httpx request, reduced to what the pipeline needs."""

    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes
    session: SessionContext | None


async def _chunks(body: bytes) -> AsyncIterator[bytes]:
    yield body


class Runtime:
    """Ledger plus pipeline for one transport; `start` is idempotent and lazy."""

    def __init__(
        self, settings: Settings, ledger_path: Path | None, cap: Money | None, *,
        client: httpx.AsyncClient | None = None, now_ms: Clock = system_ms,
    ) -> None:  # fmt: skip
        path = ledger_path or settings.ledger_path
        if path is None:
            raise ValueError("library mode uses a SQLite ledger; the config selects postgres")
        if cap is not None and cap.currency != settings.ledger_currency:
            raise ValueError(f"cap {cap.format()} is not in the ledger currency "
                             f"{settings.ledger_currency}")  # fmt: skip
        self.settings = settings
        self._path, self._cap, self._now = path, cap, now_ms
        prepare_directory(path)
        self._ledger = SqliteLedger.open(path, currency=settings.ledger_currency)
        self._owns_client = client is None
        timeout = upstream_timeout(settings.config.routing.connect_timeout_seconds)
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._pipeline: Pipeline | None = None
        self._beat: asyncio.Task[None] | None = None
        self._lock: asyncio.Lock | None = None

    async def start(self) -> Pipeline:
        """Create the scope and budget if needed, build the pipeline, begin the heartbeat."""
        self._lock = self._lock or asyncio.Lock()
        async with self._lock:
            if self._pipeline is None:
                self._pipeline = await self._assemble()
            return self._pipeline

    async def _assemble(self) -> Pipeline:
        settings = self.settings
        scope_id = await prepare_scope(self._ledger, self._path, self._cap, self._now())
        principal = LocalPrincipal(scope_id, settings.ledger_currency)
        instance = f"local-{os.getpid()}-{new_id()[:8]}"
        pipeline = Pipeline(
            settings=settings, ledger=self._ledger, keys=principal,
            providers=build_providers(settings), sender=self._client, now_ms=self._now,
            instance=instance,
        )  # fmt: skip
        # Every fail-open option is logged at start (CR-09, ADR-0003); loudly when one is on.
        level = logging.WARNING if settings.config.fail_open.allow_remote_media else logging.INFO
        for line in settings.fail_open_summary():
            _log.log(level, "%s", line)
        if (unverified := settings.unverified_summary()) is not None:
            _log.warning("%s", unverified)
        with contextlib.suppress(Exception):  # stale holds of a crashed script, charged in full
            await self._ledger.sweep_expired(self._now())
        self._beat = asyncio.ensure_future(self._maintain(pipeline))
        return pipeline

    async def _maintain(
        self, pipeline: Pipeline, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    ) -> None:
        """Keep this process's leases alive and settle other processes' expired ones.

        Renewal and the stream watch go through the pipeline, not the ledger: only it knows which
        holds this process owns and which open streams to stop (CR-03). Then the sweep. A failed
        round never ends the loop; the next beat retries.
        """
        interval = self.settings.heartbeat_ms / 1000
        while True:
            await sleep(interval)
            with contextlib.suppress(Exception):
                await pipeline.renew_leases()
                await pipeline.watch_streams()
            with contextlib.suppress(Exception):
                await self._ledger.sweep_expired(self._now())

    async def handle(self, call: Call) -> Reply:
        pipeline = await self.start()
        headers = {k.lower(): v for k, v in call.headers.items() if k.lower() not in _CREDENTIALS}
        headers["authorization"] = _PLACEHOLDER_KEY
        if call.session is not None:
            headers[SESSION_HEADER.lower()] = call.session.id
        path = call.path
        if call.method == "GET" and (path == _MODELS or path.startswith(_MODELS + "/")):
            wanted = unquote(path[len(_MODELS) + 1 :]) or None
            return await pipeline.models(headers, wanted)
        endpoint = ENDPOINTS.get(path)
        if call.method != "POST" or endpoint is None:
            return pipeline.not_found(path)
        with contextlib.ExitStack() as scope:
            if call.session is not None:  # the verifier reads the limits from this context
                scope.enter_context(activate_session(call.session))
            return await pipeline.serve(endpoint, headers, _chunks(call.body))

    async def aclose(self) -> None:
        """Finish in-flight settlements, stop the heartbeat, close the ledger and our client."""
        if self._beat is not None:
            self._beat.cancel()
            await asyncio.gather(self._beat, return_exceptions=True)
        if self._pipeline is not None:
            await self._pipeline.aclose()
        if self._owns_client:
            await self._client.aclose()
        self._ledger.close()
