"""The assertions over a load report, and the numbers that go in the review table.

All inputs are the plain data the driver read from the ledger tables and the fake provider.
`verify` raises `AssertionError` naming the first broken guarantee; `summarise` never raises.
"""

from collections import Counter
from typing import Any

# What a client may see in a healthy gateway under this workload, by error code.
EXPECTED_CODES = {"budget_exhausted", "upstream_throttled", "upstream_unavailable", "rate_limited"}
CALL_STATUS, CALL_CODE, CALL_HOW = 1, 2, 3  # indexes into a client record


def overshoot(budget: dict[str, Any]) -> int:
    """Booked plus held money above the cap (negative: headroom left)."""
    return int(budget["spent"] + budget["held"] - budget["cap"])


def client_errors(records: list[list[Any]], *, crashed: bool) -> Counter[str]:
    """Responses no healthy run produces: gateway faults and transport errors (a killed gateway
    legitimately refuses connections in the crash run)."""
    bad: Counter[str] = Counter()
    for r in records:
        how, code, status = r[CALL_HOW], r[CALL_CODE], r[CALL_STATUS]
        if how.startswith("error:") and not crashed:
            bad[how] += 1
        elif status >= 400 and code not in EXPECTED_CODES:
            bad[f"{status}:{code}"] += 1
    return bad


def audit_snapshot(facts: dict[str, Any], acc: dict[str, Any]) -> None:
    """One look at the budgets while the run is live: I1 must hold at every committed instant."""
    acc["snapshots"] += 1
    for b in facts["budgets"]:
        bound = b["overrun"] + b["excess"]
        acc["peak_overshoot_nano"] = max(acc["peak_overshoot_nano"], overshoot(b))
        if b["spent"] + b["held"] > b["cap"] + bound or min(b["spent"], b["held"]) < 0:
            acc["violations"].append(b)


def verify_ledger(facts: dict[str, Any], *, strict_clean: bool) -> None:
    """Invariants that hold at rest, read straight from the tables."""
    assert facts["grants"] == 0, "the load world has no grants, so eff == cap"
    for b in facts["budgets"]:
        for name in ("spent", "held", "overrun", "excess"):
            assert b[name] >= 0, f"negative {name} on {b['path']}"
        bound = b["overrun"] + b["excess"]
        assert b["spent"] + b["held"] <= b["cap"] + bound, f"I1 broken on {b['path']}: {b}"
        if strict_clean:  # no estimator miss injected: nothing may pass the cap at all
            assert overshoot(b) <= 0 and bound == 0, f"overshoot without overrun: {b}"
        assert b["held"] == 0, f"held money left on {b['path']} (a hold never ended)"
        if b["overrun"] and overshoot(b) > 0:  # a real breach: strict_overrun must flag it
            assert b["review_at"] is not None, f"breach not flagged for review: {b['path']}"
    assert facts["pending"] == 0, "pending reservations remain"
    verify_reservations(facts)
    verify_items(facts)


def verify_reservations(facts: dict[str, Any]) -> None:
    for rid, state, charged, _outcome, call_sum, call_rows in facts["reservations"]:
        assert state in {"settled", "released", "expired"}, f"{rid} left {state}"
        if state != "released":  # a release with no attempt (every deployment cooling) has no row
            assert call_rows >= 1, f"{rid} ended with no calls row"
        if state == "released":
            assert charged == 0, f"{rid} released but charged {charged}"
        if state == "expired":
            assert call_sum >= charged, f"{rid}: calls rows book less than the hold charged"
        else:
            assert call_sum == charged, f"{rid}: calls rows {call_sum} != reservation {charged}"


def verify_items(facts: dict[str, Any]) -> None:
    """Booked spend of each budget equals what its finished reservations charged."""
    expected: Counter[str] = Counter()
    for budget_id, state, _n, _held, charged, item_overrun in facts["items"]:
        if state == "settled":
            expected[budget_id] += charged
        elif state == "expired":
            expected[budget_id] += charged + item_overrun  # the full hold, plus a late overrun
    for b in facts["budgets"]:
        assert b["spent"] == expected[b["id"]], f"spent != sum of charges on {b['path']}"


def verify_exhaustion(report: dict[str, Any], *, expect_all: bool) -> None:
    """After exhaustion no call reaches the provider, and every one is refused."""
    leaves = len(report["leaves"])
    if expect_all:
        assert len(report["volley_keys"]) == leaves, "not every chain was exhausted"
    assert report["volley_keys"], "the run never exhausted a budget"
    mid, end = report["stats_mid"]["per_key"], report["stats_end"]["per_key"]
    for k in report["volley_keys"]:
        assert end.get(str(k), 0) == mid.get(str(k), 0), f"upstream contacted for exhausted key {k}"
    assert report["stats_end"]["total"] == report["stats_mid"]["total"]
    statuses = Counter((s, c) for chunk in report["volley"] for s, c, _how in chunk)
    assert statuses and set(statuses) == {(402, "budget_exhausted")}, statuses


def verify_upstream_matches_ledger(report: dict[str, Any], *, crashed: bool) -> None:
    """Every request the provider saw has an attempt row (the probe call included)."""
    seen, rows = report["stats_mid"]["total"], report["facts_mid"]["attempts_sent"]
    if crashed:
        assert seen >= rows, (seen, rows)  # in-flight calls of the dead gateway have no owner row
    else:
        assert seen == rows, f"provider saw {seen} requests, ledger has {rows} attempts"


def verify(report: dict[str, Any], *, strict_clean: bool, crashed: bool = False) -> None:
    assert report["ended"] == "exhausted", f"run ended by {report['ended']}"
    audit = report["audit"]
    assert audit["snapshots"] >= 3 and not audit["violations"], audit["violations"][:3]
    if strict_clean:
        assert audit["peak_overshoot_nano"] <= 0, "booked plus held passed a cap during the run"
    verify_ledger(report["facts"], strict_clean=strict_clean)
    verify_exhaustion(report, expect_all=strict_clean)
    verify_upstream_matches_ledger(report, crashed=crashed)
    errors = client_errors(report["records"], crashed=crashed)
    assert not errors, f"unexpected client-visible failures: {dict(errors)}"
    logs = report["gateway_logs"]
    assert logs["error_lines"] == 0, "gateway logged errors"
    unknown = {k: v for k, v in logs["tracebacks"].items() if not k.endswith("ClientDisconnect")}
    assert not unknown, f"gateway tracebacks: {unknown}"
    assert report["swept"] == 0 or crashed, "the final sweep still had holds to charge"
    if crashed:
        verify_victim(report)


def verify_victim(report: dict[str, Any]) -> None:
    """Every hold the killed gateway left pending was ended by a sweeper (per owner: holds of the
    live gateways prove nothing about it)."""
    victim = report["victim"]
    assert victim["pending"], f"the gateway was killed owning no pending hold: {victim['owner']}"
    states = {rid: state for rid, state, *_ in report["facts"]["reservations"]}
    left = [rid for rid in victim["pending"] if states.get(rid) != "expired"]
    assert not left, f"{len(left)} holds of the killed gateway not swept: {left[:5]}"


def loaded_overhead(records: list[list[Any]]) -> dict[str, float]:
    """Client-seen time minus the provider delay the plan asked for, for plain 200 replies, while
    hundreds of calls are in flight (queueing included: informational, not a criterion)."""
    extra = sorted(r[4] - r[5] for r in records if r[CALL_STATUS] == 200 and not r[6])
    if not extra:
        return {}
    return {
        "n": len(extra),
        "p50_ms": extra[len(extra) // 2],
        "p99_ms": extra[int(len(extra) * 0.99)],
    }


def summarise(report: dict[str, Any]) -> dict[str, Any]:
    """Numbers for the review table."""
    records = report["records"]
    facts = report["facts"]
    states = Counter(r[1] for r in facts["reservations"])
    refused = sum(1 for r in records if r[CALL_CODE] == "budget_exhausted")
    over = [overshoot(b) for b in facts["budgets"]]
    bounds = [b["overrun"] + b["excess"] for b in facts["budgets"]]
    return {
        "client_calls": len(records), "volley_calls": sum(len(v) for v in report["volley"]),
        "upstream_calls": report["stats_mid"]["total"], "reservations": dict(states),
        "refused_budget": refused,
        "max_overshoot_nano": max(0, *over, report["audit"]["peak_overshoot_nano"]),
        "loaded_overhead": loaded_overhead(records),
        "snapshots": report["audit"]["snapshots"],
        "review_flagged_budgets": sum(1 for b in facts["budgets"] if b["review_at"] is not None),
        "max_headroom_used": max(b["spent"] + b["held"] for b in facts["budgets"]),
        "sum_overrun_bound_nano": sum(bounds), "max_bound_nano": max(bounds),
        "client_errors": dict(client_errors(records, crashed=report["crashed"])),
        "gateway_tracebacks": report["gateway_logs"]["tracebacks"],
        "error_lines": report["gateway_logs"]["error_lines"], "seconds": report["seconds"],
        "swept_at_end": report["swept"], "worst_nano": report["worst_nano"],
    }  # fmt: skip
