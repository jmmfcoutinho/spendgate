"""Wiring that belongs to the gateway, not the pipeline: adapters, observer, readiness probes.

`GatewayObserver` implements the pipeline's `Observer` over `Metrics` and the JSON call log, so
`spendgate.pipeline` never imports `gateway`.
"""

import logging
from collections.abc import Mapping
from datetime import UTC, date, datetime

from spendgate.config import Settings
from spendgate.core.outcomes import Outcome
from spendgate.gateway.logs import log_call, log_event, log_internal
from spendgate.gateway.metrics import Metrics
from spendgate.gateway.observability import ReadinessChecks
from spendgate.ledger.admin_port import AdminLedger
from spendgate.ledger.port import LedgerPort
from spendgate.pipeline.adapters import build_providers
from spendgate.pipeline.faults import InternalEvent
from spendgate.pipeline.model import (
    AttemptEvent,
    Clock,
    HeldEvent,
    KeyVerifier,
    RefusedEvent,
    Sender,
    SettledEvent,
)
from spendgate.pipeline.pipeline import Pipeline, system_ms
from spendgate.providers.port import Provider

_UNKNOWN = "unknown"


class GatewayObserver:
    """Pipeline facts to Prometheus metrics and one JSON log line per finished call."""

    def __init__(self, metrics: Metrics, logger: logging.Logger) -> None:
        self._metrics = metrics
        self._logger = logger

    def on_refused(self, event: RefusedEvent) -> None:
        model = event.model or _UNKNOWN
        self._metrics.requests.inc(outcome=event.code.value, model=model)

    def on_held(self, event: HeldEvent) -> None:
        self._metrics.hold_opened()

    def on_attempt(self, event: AttemptEvent) -> None:
        m = self._metrics
        m.upstream_latency.observe(event.latency_ms, model=event.model, deployment=event.deployment)
        if event.outcome is Outcome.THROTTLED:
            m.throttles.inc(model=event.model, deployment=event.deployment)
        if event.cooldown_ms is not None:
            m.record_cooldown(deployment=event.deployment)

    def on_settled(self, event: SettledEvent) -> None:
        m = self._metrics
        usage = event.usage
        m.hold_closed()
        m.requests.inc(outcome=event.outcome.value, model=event.model)
        scope = event.scope_path or _UNKNOWN
        if event.charged.nano:
            m.record_spend(nano=event.charged.nano, model=event.model, scope=scope)
        if event.charged.nano > event.hold.nano:
            m.record_overrun(scope=scope)
        if usage is not None:
            tokens = {"input": usage.input_tokens, "output": usage.output_tokens}
            for kind, n in (*tokens.items(), ("cached", usage.cached_input_tokens)):
                if n:
                    m.tokens.inc(n, kind=kind)
        if event.remaining is not None:
            m.set_remaining(scope=scope, nano=event.remaining.nano)
        if event.ttft_ms is not None:  # streams only
            m.ttft.observe(event.ttft_ms, model=event.model, deployment=event.deployment)
        if event.review_required:
            m.review_required.inc(model=event.model)
            log_event(
                self._logger, "usage_review_required", logging.WARNING,
                request_id=event.request_id, model=event.model, scope_path=scope,
            )  # fmt: skip
        log_call(
            self._logger, request_id=event.request_id, key_prefix_=event.key_id[:8],
            scope_path=scope, model=event.model, deployment=event.deployment,
            outcome=event.outcome, input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0, cost=event.charged,
            latency_ms=event.latency_ms, http_status=event.http_status,
        )  # fmt: skip

    def on_internal(self, event: InternalEvent) -> None:
        log_internal(self._logger, event)


def catalog_age_seconds(settings: Settings, now_ms: int) -> float | None:
    """Seconds since the catalog was last updated: the newest `updated` date of its files, else
    the newest price `effective_from`; None when it has neither (the age is unknown)."""
    stamps: list[date] = []
    for doc in settings.catalog_documents:
        updated = doc.get("updated")
        if isinstance(updated, datetime):
            updated = updated.date()
        if isinstance(updated, str):
            try:
                updated = date.fromisoformat(updated)
            except ValueError:
                updated = None
        if isinstance(updated, date):
            stamps.append(updated)
    if not stamps:
        stamps = [e.effective_from for e in settings.catalog.entries()]
    if not stamps:
        return None
    newest = datetime.combine(max(stamps), datetime.min.time(), tzinfo=UTC)
    return max(0.0, now_ms / 1000 - newest.timestamp())


def bind_catalog_metrics(metrics: Metrics, settings: Settings, now: Clock) -> None:
    """Publish the catalog sha and age now, and again before every scrape (SR-05, T-CAT-03)."""

    def refresh() -> None:
        age = catalog_age_seconds(settings, now())
        metrics.set_catalog(sha=settings.catalog_sha256, age_seconds=age)

    metrics.before_render = refresh
    refresh()


def default_readiness(ledger: AdminLedger, settings: Settings, now: Clock) -> ReadinessChecks:
    """Probes over the ledger: reachable, database clock for skew, a global kill switch.

    The kill switch is only a warning on the admin detail (ADR-0015). `kill_switch_engaged` is
    synchronous but the ledger read is not, so the reachability probe (which `/readyz` always
    awaits first) also refreshes the flag the sync probe reports: None when that read failed.
    """
    engaged: bool | None = False

    async def ledger_ok() -> bool:
        nonlocal engaged
        await ledger.get_scope("readyz-probe")  # a cheap read; raises when the store is down
        try:
            engaged = await ledger.global_kill_engaged()
        except Exception:
            engaged = None  # a warning only: the kill switch never decides readiness
        return True

    def catalog_days() -> float | None:
        age = catalog_age_seconds(settings, now())
        return None if age is None else age / 86_400

    return ReadinessChecks(
        ledger_ok,
        lambda: bool(settings.catalog_sha256),
        lambda: engaged,
        ledger.db_now_ms,
        now,
        catalog_age_days=catalog_days,
    )


def build_pipeline(  # noqa: PLR0913 - the gateway's composition root
    settings: Settings, ledger: LedgerPort, keys: KeyVerifier, sender: Sender, *,
    metrics: Metrics | None = None, logger: logging.Logger | None = None,
    providers: Mapping[str, Provider] | None = None, now_ms: Clock = system_ms,
) -> Pipeline:  # fmt: skip
    """A `Pipeline` with the default adapters, and metrics and logs when both are given."""
    observer = GatewayObserver(metrics, logger) if metrics and logger else None
    return Pipeline(
        settings=settings, ledger=ledger, keys=keys, sender=sender, now_ms=now_ms,
        providers=providers or build_providers(settings), observer=observer,
    )  # fmt: skip
