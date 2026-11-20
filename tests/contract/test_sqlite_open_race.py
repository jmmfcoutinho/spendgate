"""Several processes opening one brand-new SQLite ledger file at once must all succeed."""

import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from spendgate.core.money import Money
from spendgate.ledger import migrations
from spendgate.ledger.sqlite import SqliteLedger
from spendgate.local.setup import prepare_scope

PROCESSES = 4


def open_and_report(path: str) -> int:
    """Open (migrating if first) and return how many migrations the file records."""
    ledger = SqliteLedger.open(path)
    try:
        rows = ledger._conn.execute("SELECT count(*) FROM schema_migrations").fetchone()
        return int(rows[0])
    finally:
        ledger.close()


@pytest.mark.slow
def test_four_processes_opening_one_new_ledger_file_all_succeed_and_migrate_once(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "fresh.db")
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(PROCESSES, mp_context=context) as pool:
        counts = list(pool.map(open_and_report, [path] * PROCESSES))
    assert counts == [len(migrations.load("sqlite"))] * PROCESSES


def start_like_a_script(path: str) -> list[int]:
    """What library mode does first: open, then create the scope, key and budget if missing."""
    ledger = SqliteLedger.open(path)
    try:
        now = 1_000
        scope_id = asyncio.run(
            prepare_scope(ledger, Path(path), Money.from_amount("5", "USD"), now)
        )
        return [r.effective_cap_nano for r in asyncio.run(ledger.totals(scope_id, now))]
    finally:
        ledger.close()


@pytest.mark.slow
def test_four_first_starters_create_one_scope_and_one_budget_without_a_file_lock(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "fresh.db")
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(PROCESSES, mp_context=context) as pool:
        caps = list(pool.map(start_like_a_script, [path] * PROCESSES))
    assert caps == [[5 * 10**9]] * PROCESSES  # every starter sees the one budget, never two
