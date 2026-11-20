"""Load and overshoot: N gateway processes, 8 client processes x 50 concurrent calls.

"No overshoot": 8 processes x 50 concurrent calls against a fake provider
never take booked spend past any budget by more than the documented overrun bound, and calls
after exhaustion are refused before upstream contact. The processes are real (`spendgate serve`
over a shared SQLite file, or three instances over one PostgreSQL schema) and live in
`tests.load.driver`, outside pytest's socket guard (which only lets `postgres`/`docker` tests
use loopback); this module starts the driver and asserts on what it read from the ledger tables.
"""

from pathlib import Path

import pytest

from tests.load import checks
from tests.load.runner import load_config, run_driver

SCENARIOS = ("clean", "overrun", "crash")
BACKENDS = [
    pytest.param("sqlite", id="sqlite"),
    pytest.param("postgres", marks=pytest.mark.postgres, id="postgres"),
]


# Minutes of real processes: run with `make load`, not in the default suite.
pytestmark = pytest.mark.load


@pytest.mark.slow
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_money_guarantees_hold_under_load(
    backend: str, scenario: str, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    url = request.getfixturevalue("postgres_url") if backend == "postgres" else None
    cfg = load_config(backend, scenario, url)
    report = run_driver(cfg, f"{backend}-{scenario}", tmp_path)
    checks.verify(report, strict_clean=scenario != "overrun", crashed=scenario == "crash")
    summary = checks.summarise(report)
    assert summary["upstream_calls"] >= 100, "too little reached the provider to mean anything"
    assert summary["refused_budget"] > 0
    if scenario == "overrun":
        assert summary["sum_overrun_bound_nano"] > 0, "no estimator miss was exercised"
