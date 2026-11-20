"""Hypothesis profile and the time-bounded runner shared by every fuzz target.

Short mode (default, what `uv run pytest tests/fuzz` runs): every case gets
`SPENDGATE_FUZZ_EXAMPLES` examples (default 200). Long mode (`SPENDGATE_FUZZ_SECONDS=600`):
the cases of one target run in round-robin *rounds* of a few hundred examples until that many
wall-clock seconds have passed.

Why rounds and not one huge `max_examples`: Hypothesis has no wall-clock budget, and a single run
with a very large `max_examples` cannot be stopped cleanly or report how far it got. A round is one
ordinary `@given` run with its own `@seed(base + round)`, so a long run is reproducible (same base
seed, same rounds), each round explores fresh examples, a failure is shrunk and reported by
Hypothesis as usual, and the example count is exact. `deadline=None` because a few examples (a
megabyte of audio, a 900-deep JSON document) are legitimately slower than Hypothesis' 200 ms; the
slowness that matters is asserted by the targets themselves.
"""

import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, Phase, given, seed, settings
from hypothesis.strategies import SearchStrategy

SECONDS = float(os.environ.get("SPENDGATE_FUZZ_SECONDS", "0"))
SHORT_EXAMPLES = int(os.environ.get("SPENDGATE_FUZZ_EXAMPLES", "200"))
ROUND_EXAMPLES = int(os.environ.get("SPENDGATE_FUZZ_ROUND_EXAMPLES", "300"))
BASE_SEED = int(os.environ.get("SPENDGATE_FUZZ_SEED", "20261117"))
REPORT_DIR = os.environ.get("SPENDGATE_FUZZ_REPORT_DIR")
LONG = SECONDS > 0

settings.register_profile(
    "spendgate-fuzz",
    deadline=None,
    database=None,
    derandomize=False,
    suppress_health_check=list(HealthCheck),
    phases=(Phase.generate, Phase.shrink),
    print_blob=True,
)
settings.load_profile("spendgate-fuzz")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        # Pinned regressions of fuzz findings are ordinary tests: they stay in `make test`.
        if "tests/fuzz/" in item.nodeid and "test_regression" not in item.nodeid:
            item.add_marker(pytest.mark.fuzz)
            # Hypothesis warns when a strategy's repr is huge (the 900-deep JSON documents).
            item.add_marker(
                pytest.mark.filterwarnings("ignore::hypothesis.errors.HypothesisWarning")
            )


@dataclass(frozen=True, slots=True)
class Case:
    """One invariant: examples drawn from `strategy`, each handed to `check` (which asserts)."""

    name: str
    strategy: SearchStrategy[Any]
    check: Callable[[Any], None]


def run_fuzz(target: str, cases: Sequence[Case]) -> dict[str, Any]:
    """Run every case (short: once; long: round-robin until the time is up); returns the report."""
    started = time.monotonic()
    counts = dict.fromkeys((c.name for c in cases), 0)
    rounds = 0
    current = ""
    try:
        while True:
            for index, case in enumerate(cases):
                current = f"{case.name} (round seed {BASE_SEED + rounds * 1000 + index})"
                counts[case.name] += _round(case, BASE_SEED + rounds * 1000 + index)
                if LONG and time.monotonic() - started >= SECONDS:
                    break
            rounds += 1
            if not LONG or time.monotonic() - started >= SECONDS:
                break
    except BaseException:
        _write(target, _summary(target, started, rounds, counts, "FAIL in " + current))
        raise
    report = _summary(target, started, rounds, counts, "pass")
    print("FUZZ-REPORT " + json.dumps(report))
    _write(target, report)
    return report


def _summary(
    target: str, started: float, rounds: int, counts: dict[str, int], result: str
) -> dict[str, Any]:
    return {
        "target": target,
        "mode": "long" if LONG else "short",
        "seed": BASE_SEED,
        "seconds": round(time.monotonic() - started, 1),
        "rounds": rounds,
        "examples": sum(counts.values()),
        "examples_per_case": counts,
        "result": result,
    }


def _write(target: str, report: dict[str, Any]) -> None:
    if REPORT_DIR:
        Path(REPORT_DIR, f"{target}.json").write_text(json.dumps(report, indent=1))


def _round(case: Case, round_seed: int) -> int:
    tried = 0

    def counted(example: Any) -> None:  # noqa: ANN401 - whatever the strategy produces
        nonlocal tried
        tried += 1
        case.check(example)

    limit = ROUND_EXAMPLES if LONG else SHORT_EXAMPLES
    runner = seed(round_seed)(settings(max_examples=limit)(given(case.strategy)(counted)))
    runner()
    return tried
