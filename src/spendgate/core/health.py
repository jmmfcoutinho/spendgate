"""Deployment health: cooldown and circuit breaker in one tracker.

Time is integer epoch milliseconds throughout, like the rest of ``spendgate.core``.

One ``DeploymentHealth`` per deployment. Pure state: every method takes ``now`` and nothing
sleeps or schedules, so routing simply asks ``allow(now)`` before picking a deployment.

closed --(failure share >= threshold over >= min_requests in the window)--> open
closed/half_open --(429, honouring Retry-After)--> open
open --(cooldown elapsed)--> half_open, which lets exactly one probe through
half_open --probe success--> closed;  probe failure --> open again

Defaults follow LiteLLM's: 5 s cooldown, 50 % failures, at least 5 requests.
"""

from collections import deque
from dataclasses import dataclass
from enum import StrEnum


class HealthState(StrEnum):
    """Breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class HealthConfig:
    """Breaker tuning; ``max_cooldown_ms`` stops a hostile Retry-After parking a deployment."""

    cooldown_ms: int = 5_000
    failure_threshold_pct: int = 50
    min_requests: int = 5
    window_ms: int = 60_000
    max_cooldown_ms: int = 300_000


class DeploymentHealth:
    """Cooldown plus circuit breaker for one deployment."""

    def __init__(self, config: HealthConfig | None = None) -> None:
        self._config = config or HealthConfig()
        self._state = HealthState.CLOSED
        self._results: deque[tuple[int, bool]] = deque()  # (time, ok) while closed
        self._open_until = 0
        self._probe_out = False

    @property
    def state(self) -> HealthState:
        """Current state (``open`` stays reported until ``allow`` moves it to half-open)."""
        return self._state

    def allow(self, now: int) -> bool:
        """May a request use this deployment? In half-open, only the first caller gets True."""
        if self._state is HealthState.OPEN and now >= self._open_until:
            self._state = HealthState.HALF_OPEN
            self._probe_out = False
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

    def record_success(self, now: int) -> None:
        """Count a success; a half-open success closes the breaker.

        A result in half-open is taken as the probe's: requests started before the trip are
        indistinguishable here, and a wrong close is corrected by the next failures.
        """
        if self._state is HealthState.HALF_OPEN:
            self._state = HealthState.CLOSED
            self._results.clear()
        elif self._state is HealthState.CLOSED:
            self._add(now, ok=True)

    def record_failure(self, now: int) -> None:
        """Count a failure (5xx, timeout, connection error); may open the breaker."""
        if self._state is HealthState.HALF_OPEN:
            self._trip(now, self._config.cooldown_ms)
        elif self._state is HealthState.CLOSED:
            self._add(now, ok=False)
            if self._should_trip():
                self._trip(now, self._config.cooldown_ms)

    def record_throttled(self, now: int, retry_after_ms: int | None = None) -> None:
        """A 429: cool down immediately, for at least the provider's Retry-After."""
        wanted = max(self._config.cooldown_ms, retry_after_ms or 0)
        if self._state is HealthState.OPEN:
            self._open_until = max(self._open_until, now + self._capped(wanted))
        else:
            self._trip(now, wanted)

    def _capped(self, duration_ms: int) -> int:
        return min(duration_ms, self._config.max_cooldown_ms)

    def _trip(self, now: int, duration_ms: int) -> None:
        self._state = HealthState.OPEN
        self._open_until = now + self._capped(duration_ms)
        self._probe_out = False
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
