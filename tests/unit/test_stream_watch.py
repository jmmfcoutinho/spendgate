"""The pure rule behind mid-stream stops (protocol 2.8): which open streams must end now."""

from spendgate.core.failures import StopReason
from spendgate.core.policy import KeyPolicy, KillSwitch, KillTarget
from spendgate.pipeline.watch import StreamFacts, stop_reason

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
