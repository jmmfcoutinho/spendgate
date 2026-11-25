"""The gateway's composition root: one function builds what `serve` runs.

`spendgate serve` and the integration harness both call `build_gateway`, so the tests exercise
the wiring the server ships with (the pipeline, readiness, metrics bindings, the upkeep job and
both apps) instead of a copy of it.
"""

import logging
from dataclasses import dataclass
from typing import Unpack

from starlette.applications import Starlette

from spendgate import package_version
from spendgate.config import Settings
from spendgate.gateway.admin import create_admin_app
from spendgate.gateway.admin_http import AdminContext
from spendgate.gateway.alert_webhook import WebhookAlerts, with_alerts
from spendgate.gateway.app import create_app
from spendgate.gateway.logs import LOGGER_NAME
from spendgate.gateway.maintenance import Maintenance
from spendgate.gateway.metrics import Metrics
from spendgate.gateway.observability import ReadinessChecks
from spendgate.gateway.wiring import (
    PipelineKnobs,
    bind_catalog_metrics,
    bind_runtime_metrics,
    build_pipeline,
    default_readiness,
)
from spendgate.ledger.admin_port import AdminLedger
from spendgate.ledger.keys import KeyService
from spendgate.ledger.port import MoneyLedger
from spendgate.pipeline.assembly import system_ms
from spendgate.pipeline.model import Sender
from spendgate.pipeline.pipeline import Pipeline


@dataclass(frozen=True, slots=True)
class Gateway:
    """The two apps and the upkeep job, plus the pipeline, probes and admin context they share."""

    data_app: Starlette
    admin_app: Starlette
    maintenance: Maintenance  # also the data app's background job, started with its lifespan
    pipeline: Pipeline
    readiness: ReadinessChecks
    admin_context: AdminContext


def build_gateway(  # noqa: PLR0913 - the ports plus the optional observability
    settings: Settings,
    ledger: AdminLedger,
    keys: KeyService,
    sender: Sender,
    *,
    metrics: Metrics | None = None,
    logger: logging.Logger | None = None,
    alerts: WebhookAlerts | None = None,
    money: MoneyLedger | None = None,
    **knobs: Unpack[PipelineKnobs],
) -> Gateway:
    """Everything `serve` runs, wired once.

    `money` is the ledger the pipeline uses when it is not `ledger` itself (a test double for the
    money path); readiness, upkeep and the admin API always use `ledger`. Metrics are served by the
    admin app only, so the data app gets none. `alerts` is the budget-warning webhook poster,
    run beside the sweeper: stopped with it, but a failure in either leaves the other running.
    """
    now = knobs.get("now_ms", system_ms)
    pipeline = build_pipeline(
        settings, money or ledger, keys, sender, metrics=metrics, logger=logger, **knobs
    )
    ready = default_readiness(ledger, settings, now)
    if metrics is not None:
        bind_catalog_metrics(metrics, settings, now)
        bind_runtime_metrics(metrics, pipeline.unsettled, ledger)
    upkeep = Maintenance(  # the sweeper and retention
        ledger, settings, now, logger or logging.getLogger(LOGGER_NAME), metrics
    )
    data_app = create_app(pipeline, readiness=ready, background=with_alerts(upkeep.run, alerts))
    ctx = AdminContext(
        ledger, keys, settings, now, now(), metrics, package_version(), readiness=ready
    )
    admin_app = create_admin_app(ctx, shared_port=settings.config.server.admin is None)
    return Gateway(data_app, admin_app, upkeep, pipeline, ready, ctx)
