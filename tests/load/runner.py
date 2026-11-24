"""Run `tests.load.driver` as a child process and return its JSON report.

The seed is fixed (and recorded in the report); two env vars change a run, never the assertions:
`SPENDGATE_LOAD_SCALE` multiplies the budgets, so a bigger run takes longer, and
`SPENDGATE_LOAD_REPORT_DIR` keeps each run's raw report there (the numbers in
docs/reviews/load-v0.1.md came from such a directory).
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tests.load.rig import ROOT

SEED = int(os.environ.get("SPENDGATE_LOAD_SEED", "20261117"))
SCALE = int(os.environ.get("SPENDGATE_LOAD_SCALE", "1"))
REPORT_DIR = os.environ.get("SPENDGATE_LOAD_REPORT_DIR")

# 8 client processes x 50 concurrent calls each, as the overshoot criterion says.
LOAD = {"clients": 8, "concurrency": 50, "calls_per_client": 1_000_000, "max_seconds": 240,
        "volley_calls": 40, "rpm_limit": 30, "conc_limit": 4}  # fmt: skip
# Workload per scenario. `clean`: nothing exceeds an estimate. `overrun`: some replies report 3x
# the output tokens. `crash`: one gateway is killed with holds in flight, short leases.
SCENARIOS: dict[str, dict[str, Any]] = {
    "clean": {"mix": {"throttle": 0.03, "server_error": 0.01}},
    "overrun": {"mix": {"throttle": 0.03, "server_error": 0.01, "overreport": 0.25}},
    "crash": {
        "mix": {"throttle": 0.03, "server_error": 0.01, "max_latency_ms": 300},
        "lease_seconds": 4, "crash_after_s": 2,
    },
}  # fmt: skip
GATEWAYS = {"sqlite": 4, "postgres": 3}
# Budget size (in worst-case holds) per backend and scenario. PostgreSQL serialises every call on
# the shared budget rows, and with 8 x 50 callers over three instances its queues grow until a run
# takes minutes (docs/reviews/load-v0.1.md), so its runs are smaller and the crash run uses 4 x 25.
BASE_SCALE = {
    "sqlite": {"clean": 3, "overrun": 3, "crash": 3},
    "postgres": {"clean": 1, "overrun": 1, "crash": 1},
}
CLIENT_SIZE = {("postgres", "crash"): {"clients": 4, "concurrency": 25}}
OVERHEAD_SAMPLES = {"sqlite": 600, "postgres": 300}


def run_driver(
    cfg: dict[str, Any], name: str, tmp_path: Path, *, timeout: int = 900
) -> dict[str, Any]:
    cfg_path, out_path = tmp_path / f"{name}.cfg.json", tmp_path / f"{name}.json"
    cfg_path.write_text(json.dumps(cfg))
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "tests.load.driver", str(cfg_path), str(out_path)],
        cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False,
    )  # fmt: skip
    assert done.returncode == 0, f"driver failed:\n{done.stderr[-3000:]}"
    report: dict[str, Any] = json.loads(out_path.read_text())
    report["config"] = cfg
    if REPORT_DIR:
        Path(REPORT_DIR).mkdir(parents=True, exist_ok=True)
        (Path(REPORT_DIR) / f"{name}.json").write_text(json.dumps(report))
    return report


def load_config(backend: str, scenario: str, postgres_url: str | None) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "mode": "load", "backend": backend, "seed": SEED, "gateways": GATEWAYS[backend],
        "scale": BASE_SCALE[backend][scenario] * SCALE,
    } | LOAD | SCENARIOS[scenario] | CLIENT_SIZE.get((backend, scenario), {})  # fmt: skip
    if postgres_url:
        cfg["postgres_url"] = postgres_url
    return cfg


def overhead_config(backend: str, postgres_url: str | None) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "mode": "overhead", "backend": backend, "seed": SEED, "warmup": 100,
        "samples": OVERHEAD_SAMPLES[backend],
    }  # fmt: skip
    if postgres_url:
        cfg["postgres_url"] = postgres_url
    return cfg
