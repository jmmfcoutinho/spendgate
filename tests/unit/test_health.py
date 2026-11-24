"""Deployment health: cooldown and circuit breaker transitions on a fake clock and seeded RNG."""

import random

from hypothesis import given, settings
from hypothesis import strategies as st

from spendgate.core.health import DeploymentHealth, Draw, HealthConfig, HealthState

SHORT = HealthConfig().throttle_max_ms  # the default draw takes the longest short cooldown


def seeded(seed: int) -> Draw:
    return random.Random(seed).randint  # noqa: S311 - cooldown jitter, not a secret


def _tripped(now: int = 0, config: HealthConfig | None = None) -> DeploymentHealth:
    health = DeploymentHealth(config)
    for _ in range(5):
        health.record_failure(now)
    return health


def _state(health: DeploymentHealth) -> HealthState:
    return health.state  # a call hides earlier `is` narrowing from mypy


def test_stays_closed_below_minimum_requests() -> None:
    health = DeploymentHealth()
    for _ in range(4):
        health.record_failure(0)
    assert health.state is HealthState.CLOSED
    assert health.allow(0)


def test_opens_at_failure_threshold_over_minimum_requests() -> None:
    health = DeploymentHealth()
    for _ in range(3):
        health.record_success(0)
    health.record_failure(0)
    assert _state(health) is HealthState.CLOSED  # 1 of 4, and under the minimum
    health.record_failure(0)  # 2 of 5 = 40 %
    assert _state(health) is HealthState.CLOSED
    health.record_failure(0)  # 3 of 6 = 50 %
    assert _state(health) is HealthState.OPEN


def test_failures_outside_the_window_are_forgotten() -> None:
    health = DeploymentHealth()
    for _ in range(4):
        health.record_failure(0)
    health.record_failure(100_000)  # the first four aged out of the 60 s window
    assert health.state is HealthState.CLOSED


def test_open_blocks_until_cooldown_then_leaves_on_time() -> None:
    health = _tripped(10_000)
    assert not health.allow(14_999)
    assert health.retry_after(12_000) == 3_000
    assert health.allow(15_000)
    assert health.state is HealthState.HALF_OPEN
    assert health.retry_after(15_000) == 0


def test_half_open_allows_exactly_one_probe() -> None:
    health = _tripped()
    assert health.allow(5_000)
    assert not health.allow(5_000)
    assert not health.allow(6_000)


def test_probe_success_closes_with_fresh_history() -> None:
    health = _tripped()
    assert health.allow(5_000)
    health.record_success(5_100)
    assert health.state is HealthState.CLOSED
    assert health.allow(5_100)
    health.record_failure(5_200)  # one failure must not re-trip: history was cleared
    assert health.state is HealthState.CLOSED


def test_probe_failure_reopens_for_a_full_cooldown() -> None:
    health = _tripped()
    assert health.allow(5_000)
    health.record_failure(5_500)
    assert health.state is HealthState.OPEN
    assert not health.allow(10_400)
    assert health.allow(10_500)


def test_throttle_without_retry_after_cools_at_once_for_the_short_interval() -> None:
    health = DeploymentHealth()
    assert health.record_throttled(0) == SHORT == 500
    assert health.state is HealthState.OPEN
    assert health.retry_after(0) == 500  # not the 5 s breaker cooldown


def test_throttle_without_retry_after_is_a_seeded_jittered_draw() -> None:
    config = HealthConfig()
    first = DeploymentHealth(config, seeded(36)).record_throttled(0)
    shared = seeded(36)
    spread = [DeploymentHealth(config, shared).record_throttled(0) for _ in range(20)]
    assert first == spread[0]  # one seed, one schedule: reproducible
    assert all(config.throttle_min_ms <= ms <= config.throttle_max_ms for ms in spread)
    assert len(set(spread)) > 1  # deployments of one model do not all come back at once


def test_throttle_honours_longer_retry_after() -> None:
    health = DeploymentHealth()
    assert health.record_throttled(0, retry_after_ms=30_000) == 30_000
    assert not health.allow(29_000)
    assert health.allow(30_000)


def test_retry_after_below_the_breaker_cooldown_is_honoured_as_given() -> None:
    health = DeploymentHealth()
    health.record_throttled(0, retry_after_ms=1_000)
    assert health.retry_after(0) == 1_000  # the 5 s breaker cooldown is no floor for a 429


def test_retry_after_zero_still_cools_for_the_short_interval() -> None:
    health = DeploymentHealth(draw=lambda low, high: low)
    assert health.record_throttled(0, retry_after_ms=0) == 250


def test_a_busy_cooldown_ends_closed_without_a_probe() -> None:
    health = DeploymentHealth()
    health.record_throttled(0)
    assert not health.allow(499)
    assert health.allow(500) and health.allow(500)  # everyone, not one probe: it was only busy
    assert _state(health) is HealthState.CLOSED


def test_a_throttle_during_a_breaker_open_still_ends_in_one_probe() -> None:
    health = _tripped()
    health.record_throttled(1_000, retry_after_ms=10_000)
    assert health.allow(11_000) and not health.allow(11_000)
    assert _state(health) is HealthState.HALF_OPEN


def test_retry_after_is_capped() -> None:
    health = DeploymentHealth()
    assert health.record_throttled(0, retry_after_ms=86_400_000) == 300_000
    assert health.retry_after(0) == 300_000


def test_throttle_while_open_extends_but_never_shortens() -> None:
    health = DeploymentHealth()
    health.record_throttled(0, retry_after_ms=20_000)
    health.record_throttled(1_000)  # would end at 1_500, earlier than 20_000
    assert health.retry_after(1_000) == 19_000
    health.record_throttled(2_000, retry_after_ms=60_000)
    assert health.retry_after(2_000) == 60_000


def test_throttled_probe_reopens_honouring_retry_after() -> None:
    health = _tripped()
    assert health.allow(5_000)
    health.record_throttled(5_000, retry_after_ms=12_000)
    assert health.state is HealthState.OPEN
    assert health.retry_after(5_000) == 12_000


def test_results_while_open_are_ignored() -> None:
    health = _tripped()
    assert not health.record_success(1_000)
    assert not health.record_failure(1_000)
    assert health.state is HealthState.OPEN
    assert health.retry_after(1_000) == 4_000


@settings(max_examples=500, deadline=None)
@given(calls=st.lists(st.integers(0, 2_000), max_size=40))
def test_half_open_never_hands_out_two_probes_at_one_time(calls: list[int]) -> None:
    health = _tripped()
    now = 5_000
    grants = 0
    for gap in calls:
        now += gap
        grants += health.allow(now)
    assert grants <= 1  # no outcome is recorded, so the single probe is still out


def test_repeated_busy_429s_double_the_short_cooldown_up_to_the_breaker_cooldown() -> None:
    """Each 429 that meets a deployment just back from cooling doubles the draw: no storm."""
    health = DeploymentHealth()
    now, cooled = 0, []
    for _ in range(5):
        cooled.append(health.record_throttled(now))
        now += cooled[-1]  # back exactly when the cooldown ends, and throttled again
    assert cooled == [500, 1_000, 2_000, 4_000, 5_000]  # capped at cooldown_ms


def test_a_success_resets_the_busy_escalation() -> None:
    health = DeploymentHealth()
    health.record_throttled(0)
    assert health.record_throttled(500) == 1_000
    assert health.allow(1_500)
    assert health.record_success(1_600)
    assert health.record_throttled(1_700) == 500


def test_a_429_while_cooling_extends_without_escalating() -> None:
    health = DeploymentHealth()
    health.record_throttled(0)
    assert health.record_throttled(100) == 500  # in-flight calls answering 429 together
    assert health.retry_after(100) == 500
    assert health.record_throttled(600) == 1_000  # one earlier busy cooldown, not two


def test_a_stale_success_never_closes_a_failure_opened_breaker() -> None:
    health = _tripped()
    assert not health.record_success(6_000)  # cooldown over, but no probe was handed out
    assert _state(health) is HealthState.OPEN
    assert health.allow(6_000) and not health.allow(6_000)  # the one probe is still owed


def test_a_busy_cooldown_keeps_the_failure_window() -> None:
    """Failures, then a 429, then more failures: the breaker still trips on the share."""
    health = DeploymentHealth()
    for _ in range(4):
        health.record_failure(0)
    health.record_throttled(100)
    assert health.record_failure(700)  # after the busy cooldown: 5 of 5 in the window
    assert _state(health) is HealthState.OPEN and not health.busy
    assert health.retry_after(700) == 5_000


def test_a_success_from_a_call_sent_before_the_throttle_keeps_the_escalation() -> None:
    """A call sent before the provider began throttling says nothing about it having stopped."""
    health = DeploymentHealth()
    health.record_throttled(0)
    health.record_throttled(500)  # back and throttled again: busy since 500, streak 2
    assert health.record_success(1_600, started=400)  # counted for the breaker...
    assert health.record_throttled(1_700) == 2_000  # ...but the escalation goes on
    fresh = DeploymentHealth()
    fresh.record_throttled(0)
    fresh.record_throttled(500)
    assert fresh.record_success(1_600, started=1_550)  # sent after the trip: it has stopped
    assert fresh.record_throttled(1_700) == 500


def test_escalated_marks_a_busy_cooldown_that_already_doubled() -> None:
    health = DeploymentHealth()
    health.record_throttled(0)
    assert health.busy and not health.escalated
    health.record_throttled(500)
    assert health.escalated
