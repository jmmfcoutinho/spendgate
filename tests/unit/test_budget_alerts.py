"""Budget warnings as log records: allowlisted fields, JSON in the gateway, never raising."""

import io
import json
import logging

from spendgate.core.budgets import AlertKind, BudgetAlert, Period
from spendgate.gateway.logs import ALLOWED_FIELDS, configure_json_logging
from spendgate.pipeline.alerts import BUDGET_LOGGER, alert_fields, log_alerts

NANO = 1_000_000_000
NOV_1 = 1_793_491_200_000  # 2026-11-01T00:00:00Z
SOFT = BudgetAlert(
    AlertKind.SOFT_THRESHOLD, "b1", "acme/u1", Period.MONTH, NOV_1, 8 * NANO, 10 * NANO
)


def test_every_alert_field_is_allowlisted_and_amounts_are_decimal_strings() -> None:
    fields = alert_fields(SOFT, "EUR")
    assert set(fields) <= ALLOWED_FIELDS
    assert fields == {
        "event": "soft_threshold", "logger": BUDGET_LOGGER, "budget_id": "b1",
        "scope_path": "acme/u1", "period": "month", "period_start": "2026-11-01T00:00:00Z",
        "spent": "8.000000000", "cap": "10.000000000", "currency": "EUR",
    }  # fmt: skip


def test_a_lifetime_budget_alert_has_no_period_start() -> None:
    alert = BudgetAlert(AlertKind.BLOCKED, "b2", "acme", Period.NONE, 0, 11 * NANO, 10 * NANO)
    assert "period_start" not in alert_fields(alert, "USD")


def test_the_gateway_writes_budget_warnings_as_json_lines() -> None:
    loggers = [logging.getLogger(n) for n in (BUDGET_LOGGER, "spendgate.call")]
    saved = [(lg.handlers, lg.propagate, lg.level) for lg in loggers]
    buf = io.StringIO()
    try:
        call_logger = configure_json_logging(logging.StreamHandler(buf))
        log_alerts([SOFT], "USD")
        (line,) = (json.loads(x) for x in buf.getvalue().splitlines())
        assert line["event"] == "soft_threshold" and line["level"] == "WARNING"
        assert line["logger"] == BUDGET_LOGGER and line["budget_id"] == "b1"
        assert logging.getLogger(BUDGET_LOGGER).handlers == call_logger.handlers
    finally:
        for lg, (handlers, propagate, level) in zip(loggers, saved, strict=True):
            lg.handlers, lg.propagate = handlers, propagate
            lg.setLevel(level)


class _Broken(logging.Logger):
    def warning(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("log sink is down")


def test_a_failing_logger_never_raises_into_money_handling() -> None:
    log_alerts([SOFT, SOFT], "USD", logger=_Broken("broken"))  # no exception
