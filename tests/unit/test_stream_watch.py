"""The pure rule behind mid-stream stops (protocol 2.8): which open streams must end now; and the
read of the facts it needs when a stream opens, which fails closed (M-12, T-BLK-07)."""

import asyncio

from spendgate.core.failures import StopReason
from spendgate.core.policy import KeyPolicy, KillSwitch, KillTarget
from spendgate.ledger.port import LedgerUnavailable
from spendgate.pipeline.faults import Fault, InternalEvent
from spendgate.pipeline.model import NoopObserver
from spendgate.pipeline.watch import StreamFacts, read_facts, stop_reason

FACTS = StreamFacts(
    KeyPolicy("key-1", "scope-1"), ("scope-1", "scope-0"), "openai/gpt-6-luna", "openai", None
)


def test_a_stream_nobody_forbids_goes_on() -> None:
    other = [KillSwitch(KillTarget.MODEL, "openai/other"), KillSwitch(KillTarget.KEY, "key-2")]
    assert stop_reason(FACTS, other, now=10**12) is None


def test_every_kind_of_kill_switch_that_covers_the_stream_stops_it() -> None:
    covering = [
        KillSwitch(KillTarget.GLOBAL),
        KillSwitch(KillTarget.SCOPE, "scope-0"),  # an ancestor counts
        KillSwitch(KillTarget.KEY, "key-1"),
        KillSwitch(KillTarget.MODEL, "openai/gpt-6-luna"),
        KillSwitch(KillTarget.PROVIDER, "openai"),
    ]
    for switch in covering:
        assert stop_reason(FACTS, [switch], now=0) is StopReason.KILL_SWITCH, switch


def test_a_provider_switch_on_another_provider_leaves_the_stream_alone() -> None:
    assert stop_reason(FACTS, [KillSwitch(KillTarget.PROVIDER, "anthropic")], now=0) is None


def test_session_time_stops_a_stream_only_after_the_deadline_passes() -> None:
    timed = StreamFacts(FACTS.key, FACTS.chain_scope_ids, FACTS.model, "openai", 5_000)
    assert stop_reason(timed, [], now=5_000) is None  # admission also allows `== limit`
    assert stop_reason(timed, [], now=5_001) is StopReason.SESSION_TIME


def test_a_kill_switch_wins_over_the_session_clock() -> None:
    timed = StreamFacts(FACTS.key, FACTS.chain_scope_ids, FACTS.model, "openai", 0)
    assert stop_reason(timed, [KillSwitch(KillTarget.GLOBAL)], now=9) is StopReason.KILL_SWITCH


def test_m18_a_key_switch_on_a_key_the_stream_key_replaced_stops_it() -> None:
    rotated = StreamFacts(
        FACTS.key, FACTS.chain_scope_ids, FACTS.model, "openai", None, key_lineage=("key-0",)
    )
    assert stop_reason(rotated, [KillSwitch(KillTarget.KEY, "key-0")], 0) is StopReason.KILL_SWITCH
    assert stop_reason(FACTS, [KillSwitch(KillTarget.KEY, "key-0")], now=0) is None


class Internal(NoopObserver):
    def __init__(self) -> None:
        self.events: list[InternalEvent] = []

    def on_internal(self, event: InternalEvent) -> None:
        self.events.append(event)


class Reads:
    """A facts read that fails its first `failures` calls (or stalls) and counts every call."""

    def __init__(self, failures: int, *, stall: bool = False) -> None:
        self.failures, self.stall, self.calls = failures, stall, 0

    async def __call__(self) -> StreamFacts:
        self.calls += 1
        if self.stall:
            await asyncio.Event().wait()  # never set: only the deadline ends it
        if self.calls <= self.failures:
            raise LedgerUnavailable("database is gone")
        return FACTS


def test_m12_a_transient_watch_facts_read_failure_is_retried_once() -> None:
    reads, seen = Reads(failures=1), Internal()
    assert asyncio.run(read_facts(reads, 5.0, seen, "sg_req_1")) == FACTS
    assert reads.calls == 2
    assert [(e.request_id, e.fault.where) for e in seen.events] == [("sg_req_1", "pipeline.watch")]


def test_m12_watch_facts_unreadable_twice_fail_closed_after_exactly_one_retry() -> None:
    reads, seen = Reads(failures=10), Internal()
    fault = asyncio.run(read_facts(reads, 5.0, seen, "sg_req_1"))
    assert isinstance(fault, Fault) and fault.error_class == "LedgerUnavailable"
    assert reads.calls == 2  # bounded: one retry, no third read
    assert len(seen.events) == 1  # the retried failure; the last is the caller's to report


def test_m12_a_stalled_watch_facts_read_fails_closed_at_the_deadline() -> None:
    reads = Reads(failures=0, stall=True)
    fault = asyncio.run(read_facts(reads, 0.01, Internal(), "sg_req_1"))
    assert isinstance(fault, Fault) and fault.error_class == "TimeoutError"
    assert reads.calls == 1  # the deadline covers both tries: a stuck ledger is not asked again
