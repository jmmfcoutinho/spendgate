"""The Postgres pool's metrics hooks: each transaction's connection wait, timed out or not."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from spendgate.ledger.port import LedgerUnavailable, PoolStats
from spendgate.ledger.postgres import PostgresLedger
from spendgate.ledger.sql import Flow, q
from tests.contract.rig import CURRENCY, Rig, workdir_location

pytestmark = pytest.mark.postgres

_QUICK_POOL_S = 0.2


@pytest.fixture
def pg(postgres_url: str, tmp_path: Path, new_id: Callable[[], str]) -> Iterator[Rig]:
    made = Rig("postgres", workdir_location("postgres", tmp_path), new_id)
    yield made
    made.close()


def test_each_transaction_reports_its_wait_and_the_pool_its_size(pg: Rig) -> None:
    ledger = pg.ledger
    assert isinstance(ledger, PostgresLedger)
    waits: list[float] = []
    ledger.pool_wait = waits.append
    assert ledger.pool_stats() is None  # the pool opens on first use
    pg.run(ledger.db_now_ms())
    assert len(waits) == 1 and waits[0] >= 0
    stats = ledger.pool_stats()
    assert stats is not None and stats.in_use == 0 and stats.max_size == 10 and stats.size >= 1


def test_a_wait_that_ends_in_a_pool_timeout_is_still_reported(pg: Rig) -> None:
    small = pg.run(
        PostgresLedger.open(pg.location, currency=CURRENCY, max_size=1, pool_timeout=_QUICK_POOL_S)
    )
    waits: list[float] = []
    small.pool_wait = waits.append

    async def while_the_only_connection_is_taken() -> PoolStats | None:
        pool = await small._ensure_pool()
        async with pool.connection():
            with pytest.raises(LedgerUnavailable):
                await small.db_now_ms()
            return small.pool_stats()

    try:
        busy = pg.run(while_the_only_connection_is_taken())
    finally:
        pg.run(small.aclose())
    assert busy is not None and busy.size == busy.max_size == 1
    assert len(waits) == 1 and waits[0] >= _QUICK_POOL_S * 1000 * 0.9  # the whole queueing time


def test_in_use_counts_the_connections_lent_to_transactions(pg: Rig) -> None:
    ledger = pg.ledger
    assert isinstance(ledger, PostgresLedger)
    seen: list[int] = []

    def flow() -> Flow[int]:  # runs with the connection in hand
        stats = ledger.pool_stats()
        seen.append(stats.in_use if stats else -1)
        yield q("SELECT 1 AS one")
        return 1

    pg.run(ledger._run(flow))
    after = ledger.pool_stats()
    assert seen == [1] and after is not None and after.in_use == 0


def test_a_wait_hook_that_raises_never_fails_the_transaction(pg: Rig) -> None:
    ledger = pg.ledger
    assert isinstance(ledger, PostgresLedger)

    def broken(_ms: float) -> None:
        raise RuntimeError("metrics bug")

    ledger.pool_wait = broken
    assert pg.run(ledger.db_now_ms()) > 0
