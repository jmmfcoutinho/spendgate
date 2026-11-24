"""The Postgres pool at `/metrics`: connection waits are timed, size and use are sampled."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from spendgate.gateway.metrics import Metrics
from spendgate.gateway.wiring import PooledLedger, bind_runtime_metrics
from spendgate.ledger.postgres import PostgresLedger
from tests.contract.rig import Rig, workdir_location

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg(postgres_url: str, tmp_path: Path, new_id: Callable[[], str]) -> Iterator[Rig]:
    made = Rig("postgres", workdir_location("postgres", tmp_path), new_id)
    yield made
    made.close()


def values(metrics: Metrics) -> dict[str, float]:
    lines = metrics.render().splitlines()
    return {k: float(v) for k, _, v in (ln.rpartition(" ") for ln in lines if ln[:1] != "#")}


def test_each_transaction_times_its_pool_wait_and_the_gauges_follow_the_pool(pg: Rig) -> None:
    ledger = pg.ledger
    assert isinstance(ledger, PostgresLedger) and isinstance(ledger, PooledLedger)
    metrics = Metrics()
    bind_runtime_metrics(metrics, frozenset, ledger)
    assert "spendgate_ledger_pool_size" not in values(metrics)  # the pool opens on first use
    pg.run(ledger.db_now_ms())
    pg.run(ledger.db_now_ms())
    seen = values(metrics)
    assert seen["spendgate_ledger_pool_wait_ms_count"] == 2
    assert seen["spendgate_ledger_pool_size"] >= 1
    assert seen["spendgate_ledger_pool_in_use"] == 0  # both transactions gave theirs back
    assert seen["spendgate_ledger_pool_max"] == 10
