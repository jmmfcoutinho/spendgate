"""Several processes in library mode share one ledger file and never exceed its cap (T-LED-03).

Same pattern as `tests/contract/test_ledger_multiprocess.py`: spawned children (no inherited
connections), one SQLite file, a cap that is smaller than what the children try to spend.
"""

import multiprocessing
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import httpx
import pytest

from spendgate.config import load
from spendgate.core.money import Money
from spendgate.local import transport
from tests.fakes import FakeUpstream, FakeUsage, Script
from tests.integration.harness import ENV
from tests.integration.test_local_support import BODY, CHAT, now, settings_in

PROCESSES = 4
CALLS_EACH = 15
BIG = Script(usage=FakeUsage(input_tokens=10, output_tokens=100))  # about the size of the hold


def worker(config_dir: str, ledger: str, cap: str, calls: int) -> Counter[str]:
    """One script: `calls` requests; counts outcomes and sums the charged nano-units."""
    fake = FakeUpstream(default=BIG)
    settings = load(Path(config_dir) / "spendgate.yaml", env=ENV)  # written once, by the parent
    t = transport(
        settings, ledger_path=ledger, cap=cap, client=fake.client(),
        now_ms=now,
    )  # fmt: skip
    seen: Counter[str] = Counter()
    with httpx.Client(transport=t, base_url="http://spendgate.local") as client:
        for _ in range(calls):
            reply = client.post(CHAT, json=BODY)
            seen[str(reply.status_code)] += 1
            charged = Money.from_amount(reply.headers["x-spendgate-cost"].split()[0], "USD")
            seen["charged_nano"] += charged.nano
    return seen


def one_call_cost(tmp_path: Path) -> int:
    settings_in(tmp_path)
    probe = worker(str(tmp_path), str(tmp_path / "probe.db"), "10 USD", 1)
    return probe["charged_nano"]


@pytest.mark.slow
def test_four_scripts_on_one_ledger_never_exceed_the_cap(tmp_path: Path) -> None:
    cost = one_call_cost(tmp_path)
    room = 20  # calls' worth of money, fewer than the 60 attempts, so refusals must happen
    cap = Money(cost * room, "USD").format()
    ledger = str(tmp_path / "shared.db")
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(PROCESSES, mp_context=context) as pool:
        jobs = [
            pool.submit(worker, str(tmp_path), ledger, cap, CALLS_EACH) for _ in range(PROCESSES)
        ]
        results = [job.result(timeout=300) for job in jobs]
    total = sum(results, Counter())
    assert total["200"] <= room  # no process ever got more than the pool allowed
    assert total["200"] > 0 and total["402"] > 0  # money was spent and then refused
    assert total["200"] + total["402"] == PROCESSES * CALLS_EACH  # nothing else happened
    assert total["charged_nano"] <= cost * room  # the books never exceed the cap
