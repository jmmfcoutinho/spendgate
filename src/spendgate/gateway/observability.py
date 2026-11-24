"""Health endpoints and the /metrics endpoint factory (Starlette lives here, not in the pipeline).

`/livez` says the process runs. `/readyz` says it can serve safely: ledger answers, catalog
loaded. Clock skew against the database clock and a stale catalog are warnings, not failures
(T-CLK-03, T-CAT-03): the ledger uses its own clock, so skew degrades only local timing, and an
old price list still prices every call.

A global kill switch is a warning too, never a failure (ADR-0015): it is engaged in the shared
ledger, so it would turn every replica unready at once and the load balancer would drop them all.
The pipeline already refuses each call with `kill_switch` (503) and a JSON body clients can read;
an empty target pool would answer with the load balancer's own error instead.

On the data port `/readyz` answers with the status alone (SR-06): every application can reach it,
so it must not name the operator's state. Reasons and warnings are served to an admin key with
`usage:read` at `/admin/v1/readiness`.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from spendgate.gateway.metrics import Metrics

CLOCK_SKEW_THRESHOLD_MS = 2_000
# Warn when the price catalog is older than this. A constant, not a config key: `config/schema.py`
# belongs to another change set (SR-05); promote it to `catalog.warn_after_days` when that is free.
STALE_CATALOG_DAYS = 30
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

type LedgerProbe = Callable[[], Awaitable[bool]]
type DbClockProbe = Callable[[], Awaitable[int]]  # database epoch ms
type Now = Callable[[], int]  # local epoch ms
type Guard = Callable[[Request], Awaitable[Response | None]]


@dataclass(frozen=True, slots=True)
class ReadyStatus:
    ready: bool
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def body(self, *, detail: bool = False) -> dict[str, object]:
        """The status alone, or with reasons and warnings (admin only)."""
        status: dict[str, object] = {"status": "ready" if self.ready else "not_ready"}
        if detail:
            status |= {"reasons": list(self.reasons), "warnings": list(self.warnings)}
        return status


@dataclass(frozen=True, slots=True)
class ReadinessChecks:
    """Injected dependencies; the gateway app wires the real ones."""

    ledger_ok: LedgerProbe
    catalog_loaded: Callable[[], bool]
    kill_switch_engaged: Callable[[], bool]
    db_now_ms: DbClockProbe
    now_ms: Now
    skew_threshold_ms: int = field(default=CLOCK_SKEW_THRESHOLD_MS)
    catalog_age_days: Callable[[], float | None] | None = None  # None: age unknown, no warning
    stale_after_days: int = STALE_CATALOG_DAYS


def clock_skew_warning(db_ms: int, local_ms: int, threshold_ms: int) -> str | None:
    skew = abs(db_ms - local_ms)
    if skew > threshold_ms:
        return f"clock_skew: local clock differs from the database by {skew} ms"
    return None


async def _safe_ledger(checks: ReadinessChecks) -> bool:
    try:
        return await checks.ledger_ok()
    except Exception:
        return False


async def compute_readiness(checks: ReadinessChecks) -> ReadyStatus:
    """Pure status computation; reasons never carry exception text (it may hold a DSN)."""
    reasons: list[str] = []
    warnings: list[str] = []
    ledger_up = await _safe_ledger(checks)
    if not ledger_up:
        reasons.append("ledger_unreachable")
    if not checks.catalog_loaded():
        reasons.append("catalog_not_loaded")
    if ledger_up:
        try:
            warning = clock_skew_warning(
                await checks.db_now_ms(), checks.now_ms(), checks.skew_threshold_ms
            )
        except Exception:
            warning = "clock_probe_failed"
        if warning:
            warnings.append(warning)
        if checks.kill_switch_engaged():
            warnings.append("kill_switch_engaged: a global kill switch refuses every call")
    age = checks.catalog_age_days() if checks.catalog_age_days is not None else None
    if age is not None and age > checks.stale_after_days:
        warnings.append(f"catalog_stale: the price catalog is {int(age)} days old")
    return ReadyStatus(not reasons, tuple(reasons), tuple(warnings))


def livez_endpoint() -> Callable[[Request], Awaitable[Response]]:
    async def livez(_request: Request) -> Response:
        return JSONResponse({"status": "alive"})

    return livez


def readyz_endpoint(
    checks: ReadinessChecks, *, detail: bool = False
) -> Callable[[Request], Awaitable[Response]]:
    """Status code and `{"status"}`; `detail=True` (admin only) adds reasons and warnings."""

    async def readyz(_request: Request) -> Response:
        status = await compute_readiness(checks)
        return JSONResponse(status.body(detail=detail), status_code=200 if status.ready else 503)

    return readyz


def metrics_endpoint(
    metrics: Metrics, guard: Guard | None = None
) -> Callable[[Request], Awaitable[Response]]:
    """Serve `metrics.render()`. `guard` returns a Response to refuse (e.g. 401), or None.

    Exposure (admin port or admin key) is decided by the admin layer and plugged in here.
    """

    async def metrics_view(request: Request) -> Response:
        if guard is not None:
            refusal = await guard(request)
            if refusal is not None:
                return refusal
        return PlainTextResponse(metrics.render(), media_type=METRICS_CONTENT_TYPE)

    return metrics_view
