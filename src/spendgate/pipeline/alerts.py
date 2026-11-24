"""Budget warnings: one WARNING record on `spendgate.budget` per transition the ledger reports.

The ledger reports a soft-threshold crossing, a cap breach or a block once, in the transaction
that made it, so logging what `hold` and `settle` return needs no deduplication here. The record
carries only budget facts (ids, path, period, amounts): nothing from the request, the key or the
user. The gateway formats `spendgate_fields` as JSON through its allowlist; library mode gets the
same facts in the plain message.
"""

import logging
from collections.abc import Iterable
from datetime import UTC, datetime

from spendgate.core.budgets import BudgetAlert, Period
from spendgate.core.money import Money

BUDGET_LOGGER = "spendgate.budget"
FIELDS_ATTR = "spendgate_fields"  # the record attribute the gateway's JSON formatter reads
_log = logging.getLogger(BUDGET_LOGGER)


def _amount(nano: int, currency: str) -> str:
    return Money(nano, currency).format().rsplit(" ", 1)[0]


def alert_fields(alert: BudgetAlert, currency: str) -> dict[str, object]:
    """The structured fields of one alert; every name is in the gateway's log allowlist."""
    fields: dict[str, object] = {
        "event": alert.kind.value,
        "logger": BUDGET_LOGGER,
        "budget_id": alert.budget_id,
        "scope_path": alert.scope_path,
        "period": alert.period.value,
        "spent": _amount(alert.spent_nano, currency),
        "cap": _amount(alert.cap_nano, currency),
        "currency": currency,
    }
    if alert.period is not Period.NONE:  # a lifetime budget has a single period, no start
        start = datetime.fromtimestamp(alert.period_start / 1000, UTC)
        fields["period_start"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    return fields


def log_alerts(alerts: Iterable[BudgetAlert], currency: str, logger: logging.Logger = _log) -> None:
    """Emit one WARNING per alert. Never raises: a logging fault must not touch money handling."""
    for alert in alerts:
        try:
            fields = alert_fields(alert, currency)
            text = " ".join(f"{k}={v}" for k, v in fields.items())
            logger.warning("budget %s", text, extra={FIELDS_ATTR: fields})
        except Exception:  # noqa: S112 - the next alert may still log
            continue
