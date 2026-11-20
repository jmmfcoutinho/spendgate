"""GatewayObserver feeds remaining budget, TTFT and review-required from a SettledEvent."""

import io
import logging

from spendgate.core.money import Money
from spendgate.core.outcomes import Outcome
from spendgate.gateway.logs import JsonFormatter
from spendgate.gateway.metrics import Metrics
from spendgate.gateway.wiring import GatewayObserver
from spendgate.pipeline.model import SettledEvent


def event(**kw: object) -> SettledEvent:
    base: dict[str, object] = {
        "request_id": "sg_req_1", "key_id": "k1234567", "scope_id": "s", "endpoint": "/v1/chat",
        "model": "m", "deployment": "d", "outcome": Outcome.OK, "charged": Money(5, "USD"),
        "hold": Money(9, "USD"), "usage": None, "latency_ms": 10, "released": False,
        "scope_path": "org:a",
    }  # fmt: skip
    return SettledEvent(**(base | kw))  # type: ignore[arg-type]


def test_settled_event_fields_reach_metrics_and_a_warning_is_logged() -> None:
    metrics, buf = Metrics(), io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test.admin.observer")
    logger.handlers, logger.propagate = [handler], False
    obs = GatewayObserver(metrics, logger)
    obs.on_held(None)  # type: ignore[arg-type]
    obs.on_settled(event())
    text = metrics.render()
    assert 'spendgate_usage_review_required_total{model="m"}' not in text
    obs.on_held(None)  # type: ignore[arg-type]
    obs.on_settled(event(remaining=Money(123, "USD"), ttft_ms=40, review_required=True))
    text = metrics.render()
    assert 'spendgate_budget_remaining_nano{scope="org:a"} 123' in text
    assert 'spendgate_time_to_first_token_ms_count{model="m",deployment="d"} 1' in text
    assert 'spendgate_usage_review_required_total{model="m"} 1' in text
    assert '"event":"usage_review_required"' in buf.getvalue()
    assert '"level":"WARNING"' in buf.getvalue()
