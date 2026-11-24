"""Step 4: the candidate deployments of each alias and their shared cooldown bookkeeping.

Candidates are the deployments of one alias (one catalog id) in config order. `DeploymentHealth`
is the only tracker of cooldown and circuit state; `Shared` adds why a deployment is out (the
`Cause`), which decides how a call that finds nothing to try is refused. The failover loop that
uses this lives in `pipeline.routing`.
"""

import math
import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from spendgate.config import ResolvedDeployment, Route
from spendgate.core.health import DeploymentHealth, Draw, HealthConfig
from spendgate.providers.port import AuthMode, Deployment

_MS = 1000


class Cause(StrEnum):
    THROTTLED = "throttled"
    QUOTA = "quota"  # a free 429 that means the account's quota or billing, not a busy model
    UNREACHABLE = "unreachable"
    FAILING = "failing"


@dataclass(slots=True)
class Shared:
    """Health of one real endpoint, shared by every alias that routes to it."""

    health: DeploymentHealth
    cause: Cause | None = None


@dataclass(frozen=True, slots=True)
class Target:
    label: str  # `<alias>/<index>`: stable, free of secrets, written to the calls table
    provider: str
    deployment: Deployment
    shared: Shared


class Router:
    """Ordered candidates per alias plus the cooldown bookkeeping."""

    def __init__(
        self, routes: Mapping[str, Route], config: HealthConfig, draw: Draw | None = None
    ) -> None:
        self._config = config
        # Cooldown jitter only, never a secret: a seeded `random.Random(n).randint` in tests.
        draw = draw or random.Random().randint  # noqa: S311
        shared: dict[ResolvedDeployment, Shared] = {}
        self._targets: dict[str, tuple[Target, ...]] = {}
        for alias, route in routes.items():
            self._targets[alias] = tuple(
                Target(
                    f"{alias}/{i}",
                    d.provider,
                    _deployment(d),
                    shared.setdefault(d, Shared(DeploymentHealth(config, draw))),
                )
                for i, d in enumerate(route.deployments)
            )

    def targets(self, alias: str) -> tuple[Target, ...]:
        return self._targets[alias]

    def pick(self, targets: Iterable[Target], tried: set[str], now: int) -> Target | None:
        """The first untried deployment health allows (a half-open probe is taken only here)."""
        for target in targets:
            if target.label not in tried and target.shared.health.allow(now):
                return target
        return None

    def cool(self, target: Target, now: int, retry_after_s: int | None, cause: Cause) -> int:
        """Put the deployment into cooldown (honouring Retry-After, capped); returns ms.

        A quota or billing fault will not clear in a moment: it cools for the full breaker
        cooldown, as a busy deployment's short no-hint cooldown would only invite futile retries.
        """
        hint = retry_after_s * _MS if retry_after_s is not None else 0
        if cause is Cause.QUOTA:
            hint = max(hint, self._config.cooldown_ms)
        cooldown = target.shared.health.record_throttled(now, hint)
        target.shared.cause = cause
        return cooldown

    # A result the tracker ignored (the deployment is already cooling) keeps the cause: a call
    # that started before a 429 and answers after it must not turn the cooldown into an outage.
    def failed(self, target: Target, now: int) -> None:
        if target.shared.health.record_failure(now):
            target.shared.cause = Cause.FAILING

    def succeeded(self, target: Target, now: int) -> None:
        if target.shared.health.record_success(now):
            target.shared.cause = None

    def soonest(self, targets: Iterable[Target], tried: set[str], now: int) -> int | None:
        """Ms until the first untried, busy deployment is back from its cooldown; None if none.

        Left out: a quota fault (waiting will not fix billing) and a half-open deployment whose
        probe is out, which reports 0 (nobody knows when the probe ends; waiting would spin).
        """
        waits = [
            t.shared.health.retry_after(now)
            for t in targets
            if t.label not in tried and t.shared.cause is not Cause.QUOTA
        ]
        return min((ms for ms in waits if ms > 0), default=None)

    def wait_seconds(self, targets: Iterable[Target], now: int) -> int:
        """Whole seconds until the soonest deployment may be tried again (at least 1)."""
        waits = [t.shared.health.retry_after(now) for t in targets]
        ms = min(waits, default=0) or self._config.cooldown_ms
        return max(math.ceil(ms / _MS), 1)

    def all_throttled(self, targets: Iterable[Target]) -> bool:
        return all(t.shared.cause in (Cause.THROTTLED, Cause.QUOTA) for t in targets)

    def all_quota(self, targets: Iterable[Target]) -> bool:
        return all(t.shared.cause is Cause.QUOTA for t in targets)


def _deployment(d: ResolvedDeployment) -> Deployment:
    return Deployment(
        d.provider, d.base_url, d.model, AuthMode(d.auth), d.region, d.api_version,
        d.credential.reveal() if d.credential is not None else "",
    )  # fmt: skip
