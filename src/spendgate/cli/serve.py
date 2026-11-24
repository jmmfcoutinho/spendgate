"""`serve`, `migrate` and `check-config`: the commands that start or inspect the process itself."""

import asyncio
import contextlib
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from types import FrameType
from typing import Literal

import httpx
import uvicorn

from spendgate import package_version
from spendgate.cli.client import EXIT_CONFIG, EXIT_OK
from spendgate.cli.runtime import Runtime, open_runtime, system_ms
from spendgate.config import ConfigError, Settings, check_config, load
from spendgate.config.schema import Server
from spendgate.gateway.admin import combine, create_admin_app
from spendgate.gateway.admin_http import AdminContext
from spendgate.gateway.app import create_app
from spendgate.gateway.bootstrap import BootstrapReport, apply_bootstrap
from spendgate.gateway.logs import configure_json_logging
from spendgate.gateway.maintenance import Maintenance
from spendgate.gateway.metrics import Metrics
from spendgate.gateway.wiring import bind_catalog_metrics, build_pipeline, default_readiness
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
    for line in report.unmatched_filters:
        print(f"warning: {line}", file=sys.stderr)
    if report.ok:
        print("configuration ok")
    return EXIT_OK if report.ok else EXIT_CONFIG


def print_bootstrap(report: BootstrapReport) -> None:
    """Created rows, and each new key's secret exactly once (stderr: stdout may be a pipe)."""
    out = sys.stderr
    for path in report.scopes:
        print(f"bootstrap: created scope {path}", file=out)
    for budget in report.budgets:
        print(f"bootstrap: created budget {budget}", file=out)
    for key in report.keys:
        print(f"bootstrap: created {key.kind} key {key.name} ({key.scope}): {key.secret}", file=out)


async def _migrate(settings: Settings, *, bootstrap: bool) -> None:
    async with open_runtime(settings) as rt:  # opening applies the migrations
        print(f"ledger ready ({settings.config.ledger.backend.kind}, {settings.ledger_currency})")
        if bootstrap:
            print_bootstrap(await apply_bootstrap(settings, rt.ledger, rt.keys, rt.now()))


def run_migrate(path: str, *, bootstrap: bool) -> int:
    settings = load_settings(path)
    if isinstance(settings, int):
        return settings
    asyncio.run(_migrate(settings, bootstrap=bootstrap))
    return EXIT_OK


# --- serve -----------------------------------------------------------------------------------


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
    for line in settings.fail_open_summary():
        print(f"fail_open: {line}", file=sys.stderr)
    if (unverified := settings.unverified_summary()) is not None:
        print(f"warning: {unverified}", file=sys.stderr)
    for line in settings.unmatched_filters():
        print(f"warning: {line}", file=sys.stderr)
    metrics, logger = Metrics(), configure_json_logging()
    timeout = upstream_timeout(settings.config.routing.connect_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as sender:
        pipeline = build_pipeline(
            settings, rt.ledger, rt.keys, sender, metrics=metrics, logger=logger
        )
        ready = default_readiness(rt.ledger, settings, rt.now)
        bind_catalog_metrics(metrics, settings, rt.now)
        # Metrics are served by the admin app only (see gateway.admin): not given to the data app.
        upkeep = Maintenance(rt.ledger, settings, rt.now, logger)  # sweeper + retention
        data_app = create_app(pipeline, readiness=ready, background=upkeep.run)
        ctx = AdminContext(
            rt.ledger, rt.keys, settings, rt.now, rt.now(), metrics, package_version(),
            readiness=ready,
        )  # fmt: skip
        separate = settings.config.server.admin
        admin_app = create_admin_app(ctx, shared_port=separate is None)
        main, server = parse_listen(settings.config.server.listen), settings.config.server
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
    return uvicorn.Config(
        app,  # type: ignore[arg-type]
        at.host,
        at.port,
        lifespan=lifespan,
        timeout_keep_alive=server.keep_alive_seconds,
        timeout_graceful_shutdown=server.graceful_shutdown_seconds,
    )


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


def run_serve(path: str, *, bootstrap: bool) -> int:
    settings = load_settings(path)
    if isinstance(settings, int):
        return settings

    async def main() -> None:
        async with open_runtime(settings) as rt:
            await _serve(rt, bootstrap=bootstrap)

    asyncio.run(main())
    return EXIT_OK
