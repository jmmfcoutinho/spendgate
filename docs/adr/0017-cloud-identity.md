# ADR-0017: Cloud identity, secret reload on SIGHUP, ledger pool settings

**Status:** Accepted; extended by [ADR-0035](0035-azure-entra-from-config.md) (`credential: entra`
for the Azure adapters)
**Date:** 2026-11-24

## Context

Issue #33 asks for deployments that hold no long-lived cloud secret. Three gaps stood in the way:

- Vertex AI accepted only a service-account JSON key. On GKE, Cloud Run or Compute Engine the
  workload already has an identity (workload identity, the metadata server), and exporting a key
  for it is the practice Google advises against.
- The Postgres ledger logged in with the password in its DSN. Amazon RDS, Azure Database for
  PostgreSQL and Cloud SQL can instead accept a short-lived token from the host's identity as the
  password, but only per login: the token must be fetched when a connection opens, not once at
  start.
- Rotating a `file:` secret (a provider key, the database password) needed a restart, which drains
  every open stream. The pool sizes and the pool wait were constants (1, 10, 10 s), so an operator
  could not size `max_connections` for several instances.

## Decision

- **`credential: adc`** on a `google` provider or deployment with `auth: bearer_token` uses
  `google.auth.default(scopes=[cloud-platform])` instead of a service-account key. The project is
  the credentials' own `project_id`, else the one `default()` returned; without either the call is
  refused before it is sent. The token is refreshed in a worker thread by the same off-loop helper
  as service-account tokens (`providers/refresh.py`), and all `adc` deployments share one cached
  token. `adc` anywhere else is a config error.
- **`ledger.backend.auth: {kind: rds_iam | entra | cloudsql_iam}`** makes every new Postgres
  connection log in with a token: an RDS IAM auth token (botocore `generate_db_auth_token`, the
  standard AWS chain, optional `region`), an Entra ID token (`DefaultAzureCredential`, optional
  `scope`, default the Azure Database for PostgreSQL audience), or a Google access token for Cloud
  SQL IAM sign-in (application default credentials, `sqlservice.login` scope). The DSN must name
  the user and carry no password; anything else refuses to start (exit code 3). A token is fetched
  in a worker thread, one fetch at a time (callers share the fetch in flight, shielded so a
  cancelled caller cannot start a second one), and reused until 5 minutes before it expires or
  until a connect fails. libpq reports a rejected login (28P01, 28000) as an `OperationalError`
  that often carries no SQLSTATE, so any failed connect forgets the cached token. A failed fetch is
  `ledger_unavailable`, naming the kind and the exception class only. A kind whose extra is not
  installed is refused at start (exit code 3), naming the extra.
- **Connection errors are fixed text.** A DSN that is not a valid connection string is refused
  without quoting it, and a failed connect, at start or in the pool, is reported as `cannot
  connect to the ledger database (<class>)` plus the SQLSTATE when there is one, with no chained
  exception: libpq's message quotes host, port and option values from the URL. When the host,
  port or database name contains `@` (an unencoded `@` in the password), a fixed hint says to
  percent-encode it.
- **The token libraries are imported by the ledger.** `ledger.postgres_auth` imports botocore,
  azure-identity and google-auth, each lazily and only for its kind. This is an exception to the
  rule that provider SDKs are imported only inside their own adapter (architecture.md section
  4): the database login is not a provider call, and routing it through an adapter would make
  the ledger depend on `providers`.
- The pool reads its login per connection: psycopg_pool 3.3 accepts a callable `conninfo` and an
  async callable `kwargs`, so the DSN is read from the ledger and the token source is awaited each
  time a connection opens. The migration connection asks the token source too.
- **SIGHUP** in `serve` re-reads every `file:` secret in a daemon thread of its own, so a slow mount
  never blocks the event loop, and shutdown stops reloads and abandons a read still under way
  instead of waiting for it; signals during a reload coalesce into one more read after it. The
  results are applied on the loop: provider credentials (the new router targets are built first,
  then swapped in with one assignment, so a call in flight keeps the credential it started with) and
  the ledger DSN (handed to connections opened from then on). A DSN may change only in its password;
  a new host, port, database, user or option is refused and the old DSN kept, because a silent move
  to another ledger with holds open would break the hold/settle pairing. `env:` secrets, the key
  pepper and every non-secret setting are not reloaded. A file that cannot be read keeps its old
  value. Each reload logs `secrets_reloaded` with the config paths read and changed (new log fields
  `reloaded` and `changed`), and `secret_reload_failed` per failure; values never.
- **`ledger.pool_min`** (default 1, may be 0), **`ledger.pool_max`** (default 10) and
  **`ledger.pool_timeout_seconds`** (default 10) replace the constants, with the same defaults.
  `pool_min` above `pool_max` is a config error. SQLite ignores them.
- **No new dependency and no new extra.** Each token kind uses the library of the extra an
  operator of that cloud already installs: `bedrock` (botocore), `azure` (azure-identity),
  `google` (google-auth). The `postgres` extra gains a lower bound `psycopg-pool>=3.3.0` on a
  package `psycopg[pool]` already pulled in, for the callable `conninfo` and `kwargs`. The base
  install still has 5 runtime dependencies.

## Alternatives considered

- **A `cloud` extra, or boto3.** A separate extra would duplicate the provider extras' libraries
  and add one more name to choose; botocore already signs the RDS token, boto3 adds nothing.
- **A connection `configure` hook that sets the password.** It runs after login, too late; the
  password must be part of the connect arguments.
- **Reloading the whole config on SIGHUP.** Routes, budgets and limits feed the price check and
  the hold; changing them under live holds needs its own design. Secrets are the case operators
  rotate on a schedule.
- **Reloading the key pepper.** A new pepper voids every key at once; it stays a restart.

## Consequences

- New config keys: `credential: adc`, `ledger.backend.auth` (three kinds), `ledger.pool_min`,
  `ledger.pool_max`, `ledger.pool_timeout_seconds`. Defaults keep today's behaviour exactly.
- New log events `secrets_reloaded` and `secret_reload_failed`, and allowlisted log fields
  `reloaded` and `changed` (config paths only).
- No new error codes or endpoints, no ledger schema change, no new runtime dependency.
- Money is not touched: a token or a reloaded secret changes only how spendgate authenticates; a
  failed token fetch fails closed as `ledger_unavailable` before any hold.
- Documented in [operations.md](../operations.md#cloud-identity) and
  [configuration.md](../configuration.md).
