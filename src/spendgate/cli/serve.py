"""`serve`, `migrate` and `check-config`: the commands that start or inspect the process itself."""

import asyncio
import contextlib
import importlib.util
import logging
import os
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from types import FrameType
from typing import Literal

import httpx
import uvicorn
from uvicorn.logging import DefaultFormatter

from spendgate.cli.bootstrap import migrate_bootstrap, print_bootstrap
from spendgate.cli.client import EXIT_CONFIG, EXIT_OK, EXIT_USAGE, CliError
from spendgate.cli.reload import SecretReload, on_sighup
from spendgate.cli.runtime import Runtime, open_runtime, system_ms
from spendgate.config import ConfigError, Settings, check_config, load
from spendgate.config.schema import Server
from spendgate.gateway.admin import combine
from spendgate.gateway.alert_webhook import webhook_alerts
from spendgate.gateway.bootstrap import apply_bootstrap
from spendgate.gateway.compose import build_gateway
from spendgate.gateway.logs import configure_json_logging
from spendgate.gateway.metrics import Metrics
from spendgate.pipeline.model import Sender
from spendgate.pipeline.tracing import Tracer
from spendgate.pipeline.transport import upstream_timeout


def load_settings(path: str) -> Settings | int:
    """Settings, or the exit code after printing why not."""
    try:
        return load(path, env=os.environ, now_ms=system_ms())
    except ConfigError as exc:
        return config_failed(path, exc)


def config_failed(path: str, exc: ConfigError) -> int:
    """Print each issue as `file: where: message`; the exit code for a bad configuration."""
    for where, message in exc.issues:
        print(f"{path}: {where}: {message}" if where else f"{path}: {message}", file=sys.stderr)
    return EXIT_CONFIG


def run_check_config(path: str) -> int:
    report = check_config(path, env=os.environ, now_ms=system_ms())
    # An unpriced routed model is already one of the errors (`models.X.catalog_id`): not repeated.
    for error in report.errors:
        print(f"error: {error}", file=sys.stderr)
    if report.unverified is not None:
        print(f"warning: {report.unverified}", file=sys.stderr)
    if report.superseded is not None:
        print(f"warning: {report.superseded}", file=sys.stderr)
    for line in (*report.unmatched_filters, *report.unlisted_regions):
        print(f"warning: {line}", file=sys.stderr)
    if report.ok:
        print("configuration ok")
    return EXIT_OK if report.ok else EXIT_CONFIG


async def _migrate(
    settings: Settings, *, bootstrap: bool, secrets_file: str | None, interactive: bool
) -> None:
    async with open_runtime(settings, migrate=True) as rt:  # opening applies the migrations
        print(f"ledger ready ({settings.config.ledger.backend.kind}, {settings.ledger_currency})")
        if bootstrap:
            await migrate_bootstrap(rt, secrets_file=secrets_file, interactive=interactive)


def run_migrate(path: str, *, bootstrap: bool, secrets_file: str | None = None) -> int:
    if secrets_file is not None and not bootstrap:
        raise CliError(EXIT_USAGE, "--secrets-file needs --bootstrap")
    settings = load_settings(path)
    if isinstance(settings, int):
        return settings
    interactive = sys.stderr.isatty()  # where the secrets would be printed (ADR-0032)
    asyncio.run(
        _migrate(settings, bootstrap=bootstrap, secrets_file=secrets_file, interactive=interactive)
    )
    return EXIT_OK


# --- serve -----------------------------------------------------------------------------------

OTEL_MISSING = (
    "warning: observability.otel.enabled is set but the otel extra is not installed "
    "(pip install 'spendgate[otel]'); tracing is off"
)


# Every module `gateway.otel` needs: the SDK and the OTLP/HTTP exporter it imports at start.
OTEL_MODULES = ("opentelemetry.sdk", "opentelemetry.exporter.otlp.proto.http")


def otel_installed() -> bool:
    # find_spec imports the parent package, so without it this raises instead of returning None.
    try:
        return all(importlib.util.find_spec(name) is not None for name in OTEL_MODULES)
    except ImportError:
        return False


@contextlib.asynccontextmanager
async def upstream(settings: Settings) -> AsyncIterator[tuple[Sender, Tracer | None]]:
    """The provider client, traced when `observability.otel` is on and the extra is installed."""
    timeout = upstream_timeout(settings.config.routing.connect_timeout_seconds)
    otel = settings.config.observability.otel
    if otel.enabled and not otel_installed():
        print(OTEL_MISSING, file=sys.stderr)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if not (otel.enabled and otel_installed()):
            yield client, None
            return
        from spendgate.gateway import otel as tracing  # noqa: PLC0415 - the optional extra

        started = tracing.start(otel.service_name, trust_inbound=otel.trust_inbound_context)
        try:
            yield tracing.TracingSender(client), started.tracer
        finally:
            started.shutdown()


class _Follower(uvicorn.Server):
    """A second server in the same process: the first one owns the signal handlers."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class _Leader(uvicorn.Server):
    """The data server. It hands every signal to the admin server too, so both drain at once and
    the stop takes one drain deadline, not two in a row."""

    def __init__(self, config: uvicorn.Config, follower: uvicorn.Server | None = None) -> None:
        super().__init__(config)
        self._follower = follower

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        super().handle_exit(sig, frame)
        if self._follower is not None:
            self._follower.handle_exit(sig, frame)  # same rules: a SIGINT during the drain forces


@dataclass(frozen=True, slots=True)
class Listen:
    host: str
    port: int


def parse_listen(value: str) -> Listen:
    host, _, port = value.rpartition(":")
    return Listen(host.strip("[]"), int(port))


def print_start_warnings(settings: Settings) -> None:
    """What `serve` prints at start besides errors: fail-open options and every warning."""
    for line in settings.fail_open_summary():
        print(f"fail_open: {line}", file=sys.stderr)
    if (unverified := settings.unverified_summary()) is not None:
        print(f"warning: {unverified}", file=sys.stderr)
    for line in (*settings.unmatched_filters(), *settings.unlisted_regions()):
        print(f"warning: {line}", file=sys.stderr)


async def _serve(rt: Runtime, *, bootstrap: bool) -> None:
    settings = rt.settings
    if bootstrap:
        # Scopes and budgets only: a key secret printed here would sit in the container log (SR-08).
        print_bootstrap(
            await apply_bootstrap(settings, rt.ledger, rt.keys, rt.now(), create_keys=False)
        )
        if settings.config.bootstrap.keys:
            print(
                "bootstrap: keys are created only by `spendgate migrate --bootstrap`, not by serve",
                file=sys.stderr,
            )
    print_start_warnings(settings)
    metrics, logger = Metrics(), configure_json_logging()
    async with (
        upstream(settings) as (sender, tracer),
        webhook_alerts(settings.alert_webhook, metrics, logger, rt.now) as alerts,
    ):
        gateway = build_gateway(
            settings,
            rt.ledger,
            rt.keys,
            sender,
            metrics=metrics,
            logger=logger,
            alerts=alerts,
            tracer=tracer,
            now_ms=rt.now,
        )
        # SIGHUP re-reads `file:` secrets (provider credentials, the ledger DSN) in place.
        reload = SecretReload(settings, gateway.pipeline, logger, rt.rotate_dsn)
        separate = settings.config.server.admin
        data_app, admin_app = gateway.data_app, gateway.admin_app
        main, server = parse_listen(settings.config.server.listen), settings.config.server
        with on_sighup(asyncio.get_running_loop(), reload):
            if separate is None:
                await _run(combine(data_app, admin_app), main, None, None, server)
            else:
                await _run(data_app, main, admin_app, parse_listen(separate.listen), server)


def uvicorn_config(
    app: object, at: Listen, server: Server, *, lifespan: Literal["on", "off"]
) -> uvicorn.Config:
    """One listener's uvicorn settings: keep-alive and the drain deadline come from `server`.

    At the deadline uvicorn cancels the calls still running; the pipeline settles each one as a
    cancel (M-13), so the deadline must end before the orchestrator kills the process.
    """
    _keep_uvicorn_messages()
    return uvicorn.Config(
        app,  # type: ignore[arg-type]
        at.host,
        at.port,
        lifespan=lifespan,
        timeout_keep_alive=server.keep_alive_seconds,
        timeout_graceful_shutdown=server.graceful_shutdown_seconds,
        log_config=None,  # uvicorn's dictConfig would close the handlers spendgate attached
        access_log=False,  # spendgate logs each call itself; uvicorn's line has the query string
    )


def _keep_uvicorn_messages() -> None:
    """uvicorn's own start, stop and error lines, to stderr as its default config would send them.

    Set here rather than through uvicorn's `log_config`: that one runs `logging.config.dictConfig`,
    which closes every handler already attached, the budget webhook among them.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(DefaultFormatter("%(levelprefix)s %(message)s"))
    root = logging.getLogger("uvicorn")
    root.handlers, root.propagate = [handler], False
    root.setLevel(logging.INFO)


async def _run(
    data: object, main: Listen, admin: object | None, admin_at: Listen | None, server: Server
) -> None:
    if admin is None or admin_at is None:
        await _Leader(uvicorn_config(data, main, server, lifespan="on")).serve()
        return
    second = _Follower(uvicorn_config(admin, admin_at, server, lifespan="off"))
    first = _Leader(uvicorn_config(data, main, server, lifespan="on"), second)
    task = asyncio.ensure_future(second.serve())
    try:
        await first.serve()
    finally:
        second.should_exit = True  # the data server stopped without a signal (a startup error)
        await task


def run_serve(path: str, *, bootstrap: bool, migrate: bool = False) -> int:
    """`migrate` is `--migrate`; without it the config's `ledger.auto_migrate` decides."""
    settings = load_settings(path)
    if isinstance(settings, int):
        return settings

    async def main() -> None:
        async with open_runtime(settings, migrate=True if migrate else None) as rt:
            await _serve(rt, bootstrap=bootstrap)

    asyncio.run(main())
    return EXIT_OK
