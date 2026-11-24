# ADR-0013: Safer defaults for agents: `session_required`, a new-session bound and a concurrency limit

**Status:** Accepted
**Date:** 2026-11-22

## Context

Issue #29: the defaults left an agent more room than the session design suggests.

- Session limits apply only to calls that send `X-Spendgate-Session`. An agent that drops the
  header runs with no session limit at all, bounded only by the key's parent budgets.
- `max_new_sessions_per_hour` had no default, so a key with `session_defaults` could start a new
  session, with fresh step and time limits, on every call by rotating the id.
- `defaults.limits.concurrency` had no default, so a key without its own limit could have any
  number of calls in flight. Calls in flight are what can push a budget past its cap: admission
  needs room for the whole hold, so only calls already admitted can settle above their holds.

## Decision

- New key option `session_required` (boolean, default `false`): `bootstrap.keys[].session_required`
  in the config, `session_required` in `POST /admin/v1/keys` and `PATCH /admin/v1/keys/{key_id}`
  (absent keeps the stored value), and `session_required` on every key the admin API returns.
  A call on such a key without `X-Spendgate-Session` is refused with the existing
  `invalid_request` code (400), `param: X-Spendgate-Session`, in `core.policy.resolve`, before
  the RPM, TPM and concurrency counters and before any hold. No new error code.
- `session_required` needs `session_defaults`: the config refuses to load without them, and the
  admin API answers `invalid_request` with `param: session_required`. The flag is stored as
  `"required": true` inside the `keys.session_defaults` JSON object (`core.sessions.SessionDefaults.required`),
  written only when true, so no column and no migration are needed and a key without the flag
  keeps the same row.
- `max_new_sessions_per_hour` defaults to 60 wherever session defaults are given (config
  `session_defaults` and the admin API's `session_defaults` object). An explicit `null` keeps it
  unlimited: the schema already allowed `null`, and an operator who wants unlimited minting says
  so in writing. 60 is one new session a minute, well above a human starting runs and well below
  an agent minting an id per call.
- `defaults.limits.concurrency` defaults to 16, the value the Compose reference and the example
  config already set. It applies to every key without its own `concurrency`, in the proxy and in
  library mode, where `guard(concurrency=...)` changes it (`None`: no bound). An explicit `null`
  keeps it unlimited. The default sits on a `DefaultLimits` model,
  so a key's own `limits` still default to unset and keep following `defaults.limits`.
- `docs/how-money-is-protected.md` states the bound this gives: the overrun past the cap at a
  crossing is at most the sum, over the calls in flight at that moment, of `actual - hold`.

## Consequences

- Public behaviour: a deployment whose config omitted `defaults.limits.concurrency` now refuses
  the 17th concurrent call of a key with no limit of its own (`rate_limited`,
  `details.limit: concurrency`). Set a key's `concurrency`, or `defaults.limits.concurrency`
  (a number or `null`), to keep more in flight.
- Keys created after the change through the config or the admin API get
  `max_new_sessions_per_hour: 60` unless they say otherwise; a PATCH that sends a new
  `session_defaults` object replaces it and gets the default too. Keys already stored keep the
  limits they were created with (bootstrap is create-only); no data migration.
  `migrate --bootstrap` warns when a configured key exists with other `session_required` or
  `session_defaults` than the config, and names the PATCH that changes it.
- `session_required` is opt-in, so no existing key changes. An older spendgate reading a key row
  with `"required": true` ignores the flag; downgrading drops the enforcement.
- The CLI `keys create` has no session options yet; agent keys with `session_required` are made
  through the config or the admin API.
