"""Deployment health: cooldown and circuit breaker in one tracker.

Time is integer epoch milliseconds throughout, like the rest of ``spendgate.core``.

One ``DeploymentHealth`` per deployment. Pure state: every method takes ``now`` and nothing
sleeps or schedules, so routing simply asks ``allow(now)`` before picking a deployment.

closed --(failure share >= threshold over >= min_requests in the window)--> open
closed/half_open --(429)--> open, for Retry-After when given, else a short random interval
open --(breaker cooldown elapsed)--> half_open, which lets exactly one probe through
open --(a 429 cooldown from closed elapsed)--> closed: the provider was busy, not broken
half_open --probe success--> closed;  probe failure --> open again

Breaker defaults follow LiteLLM's: 5 s cooldown, 50 % failures, at least 5 requests. A 429 without
Retry-After cools for only 250 to 500 ms (ADR-0021): one throttled call must not take a busy
deployment away from every other caller for seconds, and the jitter keeps the deployments of one
model from coming back in the same instant.
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

type Draw = Callable[[int, int], int]
"""A random integer in ``[low, high]`` inclusive, like ``random.Random.randint``."""


def _longest(low: int, high: int) -> int:
    """The default ``Draw``: deterministic (core holds no entropy source); routing injects one."""
    return max(low, high)


class HealthState(StrEnum):
    """Breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class HealthConfig:
    """Breaker tuning; ``max_cooldown_ms`` stops a hostile Retry-After parking a deployment."""

    cooldown_ms: int = 5_000  # breaker opens only; a 429 uses the throttle bounds or Retry-After
    failure_threshold_pct: int = 50
    min_requests: int = 5
    window_ms: int = 60_000
    max_cooldown_ms: int = 300_000
    throttle_min_ms: int = 250
    throttle_max_ms: int = 500


class DeploymentHealth:
    """Cooldown plus circuit breaker for one deployment."""

    def __init__(self, config: HealthConfig | None = None, draw: Draw = _longest) -> None:
        self._config = config or HealthConfig()
        self._draw = draw
        self._state = HealthState.CLOSED
        self._results: deque[tuple[int, bool]] = deque()  # (time, ok) while closed
        self._open_until = 0
        self._probe_out = False
        self._busy_only = False  # open only because of 429s from closed: no probe needed after

    @property
    def state(self) -> HealthState:
        """Current state (``open`` stays reported until ``allow`` moves it to half-open)."""
        return self._state

    def allow(self, now: int) -> bool:
        """May a request use this deployment? In half-open, only the first caller gets True."""
        self._expire(now)
        if self._state is HealthState.CLOSED:
            return True
        if self._state is HealthState.HALF_OPEN and not self._probe_out:
            self._probe_out = True
            return True
        return False

    def retry_after(self, now: int) -> int:
        """Milliseconds until the next attempt may be admitted (0 when allowed or probing)."""
        if self._state is HealthState.OPEN:
            return max(self._open_until - now, 0)
        return 0

    def record_success(self, now: int) -> bool:
        """Count a success; a half-open success closes the breaker. False: ignored while open.

        A result in half-open is taken as the probe's: requests started before the trip are
        indistinguishable here, and a wrong close is corrected by the next failures.
        """
        self._expire(now)
        if self._state is HealthState.HALF_OPEN:
            self._state = HealthState.CLOSED
            self._results.clear()
        elif self._state is HealthState.CLOSED:
            self._add(now, ok=True)
        return self._state is not HealthState.OPEN

    def record_failure(self, now: int) -> bool:
        """Count a failure (5xx, timeout, connection error); may open the breaker.

        False: the deployment was already open, so the result was ignored.
        """
        self._expire(now)
        if self._state is HealthState.OPEN:
            return False
        if self._state is HealthState.HALF_OPEN:
            self._trip(now, self._config.cooldown_ms)
        else:
            self._add(now, ok=False)
            if self._should_trip():
                self._trip(now, self._config.cooldown_ms)
        return True

    def record_throttled(self, now: int, retry_after_ms: int | None = None) -> int:
        """A 429: cool down at once; returns the cooldown it asked for, in ms (capped).

        Retry-After is honoured as given (up to ``max_cooldown_ms``); without one, or with a
        shorter one, the cooldown is a random draw between the throttle bounds.
        """
        short = self._draw(self._config.throttle_min_ms, self._config.throttle_max_ms)
        wanted = self._capped(max(retry_after_ms or 0, short))
        if self._state is HealthState.OPEN:  # extend, never shorten; a breaker open stays one
            self._open_until = max(self._open_until, now + wanted)
        else:
            busy = self._state is HealthState.CLOSED  # a throttled probe still owes its probe
            self._trip(now, wanted)
            self._busy_only = busy
        return wanted

    def _expire(self, now: int) -> None:
        """An elapsed cooldown: a busy one ends closed, a breaker one in half-open (one probe)."""
        if self._state is HealthState.OPEN and now >= self._open_until:
            self._state = HealthState.CLOSED if self._busy_only else HealthState.HALF_OPEN
            self._probe_out = False

    def _capped(self, duration_ms: int) -> int:
        return min(duration_ms, self._config.max_cooldown_ms)

    def _trip(self, now: int, duration_ms: int) -> None:
        self._state = HealthState.OPEN
        self._open_until = now + self._capped(duration_ms)
        self._probe_out = False
        self._busy_only = False
        self._results.clear()

    def _add(self, now: int, *, ok: bool) -> None:
        self._results.append((now, ok))
        cutoff = now - self._config.window_ms
        while self._results and self._results[0][0] <= cutoff:
            self._results.popleft()

    def _should_trip(self) -> bool:
        total = len(self._results)
        if total < self._config.min_requests:
            return False
        failures = sum(1 for _, ok in self._results if not ok)
        return failures * 100 >= self._config.failure_threshold_pct * total
