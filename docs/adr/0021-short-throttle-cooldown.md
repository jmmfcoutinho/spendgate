# ADR-0021: Short jittered throttle cooldown and a bounded wait for a cooling deployment

**Status:** Accepted
**Date:** 2026-11-24

## Context

The load test's clean scenario (issue #36) injects a provider 429 on 3% of calls, yet a fifth to
a third of all calls (712 of 2,098 in the report, 545 of 2,620 on a rerun) were refused with
`503 upstream_unavailable`. Three things combined:

- A 429 without `Retry-After` cooled its deployment for `routing.cooldown_seconds` (5 s by
  default, 1 s in the load rig), and a shorter `Retry-After` was raised to that floor. One
  throttled call took a busy deployment away from every other caller for seconds.
- When the cooldown ended the deployment went to half-open, like a tripped breaker, and let a
  single probe through; every concurrent call found nothing to try until that probe finished.
- A call sent before the 429 and answered after it cleared the deployment's cause even though the
  tracker ignored the result. With every deployment cooling, the refusal then read "every
  deployment is temporarily unavailable" (503) instead of `upstream_throttled` (429).

## Decision

- A 429 that means "busy" cools its deployment for the provider's `Retry-After` as given (still
  capped at `max_cooldown_seconds`), or, without one, for a random 250 to 500 ms. The draw comes
  from a `random.Random` the router owns (tests inject a seeded one); the jitter keeps the
  deployments of one model from coming back at the same instant. `Retry-After: 0` gets the short
  draw, never zero.
- A 429 that means quota or billing (`insufficient_quota` and the like) still cools for the full
  `cooldown_seconds`: it will not clear in a moment.
- A cooldown that started from a closed breaker ends closed: the provider was busy, not broken, so
  no single probe is needed. A breaker that opened on failures still ends in half-open with one
  probe, even when a 429 extended it.
- A result the tracker ignores (the deployment is cooling) no longer changes the deployment's
  cause, so a refusal while everything cools stays `upstream_throttled`.
- When every untried candidate is cooling, the call waits for the soonest one to come back, at
  most 1 s per call in total, and only while the wait ends before the request deadline
  (`server.max_request_seconds`); then it tries it. A longer cooldown is refused at once, as
  before. Quota faults and a half-open deployment whose probe is out are never waited for.
- The wait sends nothing and happens inside the call's one hold: failover stays free (M-01,
  M-13), and no money rule changes.

## Consequences

- Default policy changes: `cooldown_seconds` now governs breaker opens and quota 429s only; a busy
  429 cools for 250 to 500 ms or its `Retry-After`, which may now be shorter than
  `cooldown_seconds`. A call may take up to 1 s longer before it is served or refused.
- In the load test's clean scenario (SQLite, 4 gateways, 8 x 50 callers) the `503
  upstream_unavailable` refusals went from 545 to 0 on the same seed.
- A provider that throttles without `Retry-After` is retried sooner. The bound per call (one wait
  of at most 1 s, then `routing.max_attempts`) and the jitter keep that from turning into a storm.
- No new config keys, error codes, endpoints or dependencies. The bounds are constants in
  `spendgate.core.health.HealthConfig` and `spendgate.pipeline.routing`.
