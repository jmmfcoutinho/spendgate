"""`serve`, `migrate` and `check-config`: the commands that start or inspect the process itself."""

import asyncio
import contextlib
import importlib.metadata
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import uvicorn

from spendgate.cli.client import EXIT_CONFIG, EXIT_OK
from spendgate.cli.runtime import Runtime, open_runtime, system_ms
from spendgate.config import ConfigError, Settings, check_config, load
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
        for where, message in exc.issues:
            print(f"{path}: {where}: {message}" if where else f"{path}: {message}", file=sys.stderr)
        return EXIT_CONFIG


def run_check_config(path: str) -> int:
    report = check_config(path, env=os.environ, now_ms=system_ms())
    for error in report.errors:
        print(f"error: {error}", file=sys.stderr)
    for model in report.unpriced:
        print(f"warning: routed model {model} has no price in force", file=sys.stderr)
    if report.unverified is not None:
        print(f"warning: {report.unverified}", file=sys.stderr)
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
        version = importlib.metadata.version("spendgate")
        ctx = AdminContext(
            rt.ledger, rt.keys, settings, rt.now, rt.now(), metrics, version, readiness=ready
        )
        separate = settings.config.server.admin
        admin_app = create_admin_app(ctx, shared_port=separate is None)
        main = parse_listen(settings.config.server.listen)
        if separate is None:
            await _run(combine(data_app, admin_app), main, None, None)
        else:
            await _run(data_app, main, admin_app, parse_listen(separate.listen))


async def _run(data: object, main: Listen, admin: object | None, admin_at: Listen | None) -> None:
    first = uvicorn.Server(uvicorn.Config(data, main.host, main.port, lifespan="on"))  # type: ignore[arg-type]
    if admin is None or admin_at is None:
        await first.serve()
        return
    second = _Follower(uvicorn.Config(admin, admin_at.host, admin_at.port, lifespan="off"))  # type: ignore[arg-type]
    task = asyncio.ensure_future(second.serve())
    try:
        await first.serve()
    finally:
        second.should_exit = True
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
