"""`calls.http_status` (ADR-0010): provider status per attempt, range-checked, every adapter."""

import pytest

from spendgate.core.outcomes import Outcome
from spendgate.ledger.admin_port import CallFilters
from tests.contract.rig import T0, E, Rig, attempt


def test_calls_http_status_round_trips_per_attempt_and_null_is_allowed(rig: Rig) -> None:
    w = rig.world("10")
    rig.hold_ok(w, "A", "1")
    rig.settle(
        "A", "0.5",
        attempts=(
            attempt(1, Outcome.THROTTLED, 0, T0, True, http_status=429),
            attempt(2, Outcome.UPSTREAM_5XX, 0, T0, True),  # never answered: NULL
            attempt(3, Outcome.OK, E("0.5"), T0, usage_cost_nano=E("0.5"), http_status=200),
        ),
    )  # fmt: skip
    rows = rig.run(rig.ledger.get_calls("A"))
    assert [(c.attempt, c.http_status) for c in rows] == [(1, 429), (2, None), (3, 200)]
    last = rig.run(rig.ledger.get_call("A"))
    assert last is not None and last["http_status"] == 200
    listed = rig.run(rig.ledger.list_calls(CallFilters(), before=None, limit=10))
    assert sorted(str(c["http_status"]) for c in listed) == ["200", "429", "None"]


@pytest.mark.parametrize("bad", [99, 600, 0, -1])
def test_calls_http_status_check_rejects_out_of_range_values(rig: Rig, bad: int) -> None:
    w = rig.world("10")
    rig.hold_ok(w, "A", "1")
    with pytest.raises(Exception, match=r"(?i)http_status|check"):
        rig.settle("A", "0.5", attempts=(attempt(1, Outcome.OK, E("0.5"), http_status=bad,
                                                   usage_cost_nano=E("0.5")),))  # fmt: skip


@pytest.mark.parametrize("edge", [100, 599])
def test_calls_http_status_check_accepts_the_range_edges(rig: Rig, edge: int) -> None:
    w = rig.world("10")
    rig.hold_ok(w, "A", "1")
    rig.settle("A", "0.5", attempts=(attempt(1, Outcome.OK, E("0.5"), http_status=edge,
                                              usage_cost_nano=E("0.5")),))  # fmt: skip
    assert rig.run(rig.ledger.get_calls("A"))[0].http_status == edge
