"""`spendgate_overruns_total` moves when the provider reports more than the hold (C-21, T-CAT-04).

A stale catalog or a provider that changed its price shows up first as settlements above the
hold: the counter is how an operator sees that before the invoice does.
"""

import io
import logging
from pathlib import Path

from spendgate.gateway.logs import configure_json_logging
from spendgate.gateway.metrics import Metrics
from spendgate.gateway.wiring import GatewayObserver
from spendgate.pipeline.settle import SANITY_FACTOR
from tests.fakes import FakeUsage, Script
from tests.integration.harness import async_test, open_stack

SERIES = "spendgate_overruns_total{"


def overruns(metrics: Metrics) -> dict[str, float]:
    """Overrun count per series line (labels included), from the rendered exposition."""
    rows = [ln.rsplit(" ", 1) for ln in metrics.render().splitlines() if ln.startswith(SERIES)]
    return {labels: float(value) for labels, value in rows}


@async_test
async def test_t_cat_04_the_overrun_metric_moves_only_when_usage_costs_more_than_the_hold(
    tmp_path: Path,
) -> None:
    metrics = Metrics()
    observer = GatewayObserver(
        metrics, configure_json_logging(logging.StreamHandler(io.StringIO()))
    )
    async with open_stack(tmp_path, pipeline_kwargs={"observer": observer}) as s:
        within = await s.chat()  # the default usage is far below the hold
        assert within.status_code == 200
        assert overruns(metrics) == {}
        s.fake.enqueue(Script(usage=FakeUsage(input_tokens=5_000, output_tokens=100)))
        above = await s.chat()  # "hi" was estimated at a few tokens; 5,000 were billed
        assert above.status_code == 200
        rid = above.headers["x-spendgate-request-id"]
        (row,) = await s.ledger.get_calls(rid)
        assert row.charged_nano > row.hold_nano and row.overrun_nano > 0  # booked, not clamped
        assert row.charged_nano < SANITY_FACTOR * row.hold_nano  # an overrun, not absurd usage
        assert row.review_required_at is None
        assert list(overruns(metrics).values()) == [1.0]
        (labels,) = overruns(metrics)
        assert 'scope="' in labels
