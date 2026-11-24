# Operations

Day-two tasks for a running proxy: backups, keys, the kill switch, usage, retention, Postgres
connections, the sweeper, draining on restart, and reading metrics and logs. Setup is in [Running locally](deploy/local.md); every setting is in
the [configuration reference](configuration.md).

Commands talk to the admin API, so they need an admin key in `SPENDGATE_ADMIN_KEY`. The API's
address is `--url`, else `SPENDGATE_URL`, else the listener in `--config` (`server.admin.listen`,
or `server.listen` when the admin API shares the data port; a `0.0.0.0` listener is reached on
loopback), else `http://127.0.0.1:8080` when there is no config file. An address is taken from the
config only when it is on this machine (`127.0.0.0/8`, `::1`, `localhost` or a bind-all listener);
any other host exits with code 3: pass `--url` or set `SPENDGATE_URL` for a remote admin API. On the host that runs the
proxy, add `--local` instead: it opens the ledger from `--config` directly and needs no key (in
Compose, run it through `docker compose exec spendgate`). Add `--json` for machine output. These
three flags belong to the admin commands; `serve`, `migrate` and `check-config` take only
`--config`. `spendgate --version` prints the installed version.
Exit codes: 0 ok, 1 refused by the server, 2 bad command line, 3 bad configuration, 4 no or wrong
admin key, 5 cannot reach the API or the ledger.

## Backups

The ledger is the only state. It holds budgets, spend, call records and the hashes of your keys.
Back it up on a schedule you can live with; a restored ledger is exactly as true as the backup,
so spend after the backup point is lost from the books.

**SQLite.** The database runs in WAL mode, so do not copy the single file while the proxy is
running. Use SQLite's online backup, which is safe at any time and needs no extra tool:

```bash
python3 -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" \
  /data/spendgate.db /backups/spendgate-$(date +%F).db
chmod 600 /backups/spendgate-*.db
```

The proxy refuses a ledger file with wider permissions than 0600, so keep the mode on a restored
copy. To restore: stop the proxy, put the file in place, start it. Or stop the proxy and copy the
`.db`, `.db-wal` and `.db-shm` files together.

**Postgres.** Use the normal tools: `pg_dump --format=custom` on a schedule, `pg_restore` to
restore, then `spendgate migrate --config spendgate.yaml` to bring the schema to the running
version.

**Also keep** the key pepper (`server.key_pepper`) with the backup, stored separately from it.
Stored key hashes depend on it: lose or change the pepper and every key stops working.

## Keys

- **Create** (the secret is printed once and never stored in clear):
  `spendgate keys create --scope org:acme/project:app --name my-app --models gpt-6-luna`.
  Keys can also be declared under `bootstrap.keys`; `spendgate migrate --bootstrap` creates them
  (printing each secret once) and never changes them afterwards. `serve` creates the bootstrap
  scopes and budgets but never keys, so no secret reaches the server's log.
- **Where a printed secret goes:** `keys create`, `keys rotate` and `migrate --bootstrap` print it
  to the terminal. Run them interactively; in containers, as a one-off
  `docker compose run --rm spendgate …` (with `--rm` the container and its log are deleted when it
  exits). Never put them in a service's command or entrypoint, where the output stays in the
  container log. If your Docker log driver ships output elsewhere (journald, a cloud log service),
  that copy holds the secret too: delete it there, or rotate the key.
- **Rotate** with no downtime: `spendgate keys rotate KEY_ID --grace-seconds 3600`. It issues a
  replacement with the same settings; the old key keeps working for the grace period (default one
  hour, at most seven days), so you can roll the new one out first.
- **Revoke** now: `spendgate keys revoke KEY_ID`. List with `spendgate keys list`; secrets are
  never shown.
- **Provider keys** live in the proxy's environment or secret files (`env:` and `file:` references)
  and are read when the proxy starts. Rotate one by updating the secret and restarting.
- **The pepper** protects stored key hashes. Do not rotate it casually: it invalidates every key.

## Kill switch

Stops traffic without a deploy. A kill switch refuses calls with `kill_switch` (503) before any
provider contact, and ends running streams within one heartbeat.

```bash
spendgate kill on --reason "runaway job"                    # everything
spendgate kill on --scope org:acme/project:app --reason "investigating"
spendgate kill on --key KEY_ID --reason "leaked"            # one key; also --provider, --model
spendgate status                                            # lists switches and budget alerts
spendgate kill off SWITCH_ID
```

A provider-level switch refuses only when every candidate deployment of the model is killed, so
other deployments keep serving.

## Budgets and overruns

```bash
spendgate budgets list org:acme/project:app
spendgate budgets set org:acme --cap "200 EUR" --period month
spendgate budgets grant BUDGET_ID --amount "10 EUR" --until 7d --reason "launch week"
spendgate budgets ack-overrun BUDGET_ID --reason "price page was stale; catalog fixed"
```

If a settlement exceeded the hold and breached the cap, the budget is flagged `REVIEW` and refuses
calls until `ack-overrun`; read why first (the call record shows the price entry used). A grant is
extra room that expires on its own. See [how money is protected](how-money-is-protected.md).

`--model-filter` limits a budget to some models. It is a case-sensitive glob matched against the
catalog id (`provider/model`, such as `openai/gpt-6-*`), never against the alias a client sends.
A filter that matches no routed catalog id is refused (`invalid_request`, `param: model_filter`),
since that budget would never apply; in `bootstrap.budgets` it is a `check-config` warning.
So route the model first, or declare the budget in `bootstrap.budgets` next to the new route: the
check uses the routes of the instance that serves the admin call, which during a rolling deploy
may not have the new route yet.

## Usage queries

```bash
spendgate usage --by service,model --since 7d
spendgate usage --scope org:acme --by day,user --since 2026-11-01T00:00:00Z
spendgate usage --by tag --tag import --basis usage
```

`--by` takes a comma list (`scope`, `service`, `user`, `model`, `provider`, `day`, `key`, `tag`).
`--basis charged` (default) sums the money booked to budgets; `usage` sums the cost of the usage the provider reported, so a call with no reported usage adds 0 and the total is a lower bound. The same
data is `GET /admin/v1/usage`; one call is `GET /admin/v1/calls/{request_id}` (a streaming reply
carries its request id in `X-Spendgate-Request-Id`). Both need an admin key with `usage:read`
([admin API](admin-openapi.yaml)). On Postgres a report statement may run for 60 s (see
[Postgres connections](#postgres-connections)).

## Retention

Call records older than `ledger.retention_days` (default 90) are deleted by a job that `serve`
runs once a day, starting at its first pass. Whole UTC days are kept or deleted together, finished
reservations and sessions go with them, and pending holds never do. On Postgres each batch
statement may run for 60 s (see [Postgres connections](#postgres-connections)). Consequences:

- Usage reports cover the retention window only; a `since` before it is refused rather than
  silently truncated.
- Budget totals do not depend on call rows, so deleting old calls never changes a balance.
- Deletion is permanent. Export what you need to keep (`usage --json`) or back up first, before
  lowering the setting.

## Postgres connections

A call waits for its hold (the one transaction that checks and reserves its budgets) for at most
10 seconds; after that it is refused with `ledger_unavailable` (503, `Retry-After: 5`) and nothing
is sent upstream. If that transaction commits later anyway, the hold is released at 0. SQLite
waits up to 30 seconds for the file lock (`busy_timeout`); the 10-second bound applies to it too.

On Postgres, every pooled connection also gets these bounds, so a locked budget row or a stuck
server ends quickly instead of holding the call:

| Setting | Default | Effect |
|---|---|---|
| `lock_timeout` | `2s` | A row lock held longer by another session (an open `psql` transaction, a long admin change) fails the hold |
| `statement_timeout` | `5s`; `60s` for usage reports, call and audit lists and retention batches | A single statement that runs longer is cancelled |
| `idle_in_transaction_session_timeout` | `10s` | The server closes a connection left inside a transaction |
| `keepalives`, `keepalives_idle`, `keepalives_interval`, `keepalives_count` | `1`, `15`, `5`, `3` | TCP keepalives: a dead server or a dropped network is noticed in about 30 s |
| `tcp_user_timeout` | `30000` (ms) | Data the server never acknowledges (a `COMMIT` to a silent host) is given up after 30 s instead of the kernel's ~15 min of retransmission |

Lease renewal skips a hold whose row another transaction has locked instead of waiting for it, so
one long lock does not fail the heartbeat and stop every open stream. Such a row is being closed by
its owner, or a sweeper is expiring a lease that is already overdue (that stream is stopped as
`lease_lost`); if the sweep rolls back, the hold stays pending and a later sweep expires it.
Callers waiting for a pooled connection queue for up to 10 s; none is refused just for queueing,
so a burst of new calls cannot block settlements or renewals of calls already running.


A lock timeout (SQLSTATE 55P03) or a cancelled statement (57014) is refused as
`ledger_unavailable` and not retried: a row held for 2 s is most likely still held a moment
later. Deadlocks (40P01) and serialization failures (40001) are still retried, up to 3 times;
keep `lock_timeout` above the server's `deadlock_timeout` (1 s by default) so a deadlock is
reported as one. Migrations at start (`serve`, `spendgate migrate`) run without these timeouts,
because they may wait for another instance's migration. The reasoning is in
[ADR-0012](adr/0012-bounded-ledger-waits.md).

**Overriding.** A value you set wins. The timeouts are applied only where the server still
reports its built-in default, so anything set in the URL, `PGOPTIONS`, `ALTER ROLE ... SET`,
`ALTER DATABASE ... SET` or `postgresql.conf` is kept, also for reports (a `statement_timeout`
you set is not raised to 60 s for them). TCP options named in the URL are kept too. In the URL, server settings go in `options` (URL-encoded) and libpq options are plain
parameters (the password is left out of this example):

```text
postgresql://spendgate@db.internal:5432/spendgate?sslmode=verify-full&sslrootcert=/etc/spendgate/db-ca.pem&keepalives_idle=30&options=-c%20lock_timeout%3D5s%20-c%20statement_timeout%3D15s
```

**TLS.** `ledger.backend.dsn` is passed unchanged to libpq, so every libpq connection parameter
works. When the database is not on the same host or a private container network, use TLS and
verify the server with `sslmode=verify-full` and `sslrootcert`, as in the example above.
`verify-full` checks the certificate chain and the host name, so neither the password nor the
ledger traffic can be read or redirected on the way; `require` only encrypts and accepts any
certificate, and the libpq default (`prefer`) falls back to plaintext. The DSN is a secret
reference (`env:NAME` or `file:/path`), so the whole URL, password included, lives in a secret and
never in `spendgate.yaml`.

## The sweeper

A hold whose owner died (a crash, a killed container) would block budget forever, so a background
sweeper in `serve` (and in library mode) charges and releases holds whose lease expired. It runs
every `ledger.sweep_seconds` (default 30). An expired hold is charged **in full**, because a call
that may have reached the provider is paid for. After a crash, a budget therefore shows held money
for up to `ledger.lease_seconds` (default 60) plus one sweep interval, then it settles. Nothing to schedule: every instance sweeps, any of them may settle an expired hold, and a hold
still being renewed by a live instance is left alone. A failing pass is logged as `sweep_failed` and retried on the next tick, never fatal.

## Draining on restart

On `SIGTERM`, `serve` closes its listener at once and waits for every call in flight, streams
included, before it shuts down; the lease heartbeat keeps running while it waits, so those holds
are settled normally. It does not set a drain deadline of its own: the orchestrator's grace period
is the deadline. Once shutdown has started, a `SIGINT` forces an exit and a second `SIGTERM` does
nothing (uvicorn 0.54). A forced exit skips the lifespan shutdown, so the holds still open are left
to the sweeper. A call still running when the process is killed or forced out is never settled by
it; its hold expires after `ledger.lease_seconds` and the next sweep (any instance, or this one
after restart) charges it **in full**. Size the stop so that does not happen:

- **Stop sending new calls first.** The listener closes as soon as `SIGTERM` arrives, so new
  connections are refused until the load balancer drops the instance, and `/readyz` does not turn
  unready before that. That gap is why the load balancer needs time before the signal: in
  Kubernetes a `preStop` hook that waits 5 to 10 seconds, with the `sleep` lifecycle action
  (Kubernetes 1.30 and later) or `exec: {command: ["sleep", "10"]}`.
- **Grace period sized to the longest stream.** One call or stream runs at most
  `server.max_request_seconds` (default 900). Set `terminationGracePeriodSeconds` (Compose:
  `stop_grace_period`, default 10 s) to at least the `preStop` wait plus `max_request_seconds` plus
  a margin of about 30 s. In Kubernetes the `preStop` wait counts against the grace period. A
  shorter grace period is a choice to charge the longest calls in full on every restart.
- **After a kill.** Held money from the killed instance shows for up to `ledger.lease_seconds`
  (default 60) plus one `ledger.sweep_seconds` (default 30), then the sweep settles it. With one
  instance that happens after it starts again; with several, a surviving one sweeps it.

## Metrics and logs

**Metrics** are Prometheus text at `GET /metrics`, served by the admin app only: open to whoever
can reach a separate admin port, or needing an admin key with `usage:read` on a shared port.
Probes `GET /livez` and `GET /readyz` are on the data port; `/readyz` answers with the status code
and `{"status"}` only. The reasons and warnings (clock skew, a catalog older than 30 days) are at
`GET /admin/v1/readiness` with a `usage:read` admin key. Label values never come from the
request: `model` is always the routed catalog id (`unknown` on a refusal that names no routed
model), and `scope` is the key's scope path (`unknown` if it could not be read). Names:

| Metric | Meaning |
|---|---|
| `spendgate_requests_total{outcome,model}` | Requests by outcome |
| `spendgate_spend_nano_total{model,scope}` | Settled spend in nano-units of the ledger currency |
| `spendgate_budget_remaining_nano{scope}` | Remaining budget |
| `spendgate_holds_in_flight` | Pending holds right now |
| `spendgate_overruns_total{scope}` | Settlements above the hold (alert on any increase) |
| `spendgate_usage_review_required_total{model}` | Absurd provider usage, booked in full and flagged |
| `spendgate_throttles_total`, `spendgate_cooldowns_total` | Provider 429s and deployment cooldowns |
| `spendgate_upstream_latency_ms`, `spendgate_time_to_first_token_ms` | Latency histograms |
| `spendgate_tokens_total{kind}` | Tokens by kind |
| `spendgate_catalog_age_seconds`, `spendgate_catalog_info{sha}` | Age and checksum of the price catalog |

Alert on `spendgate_overruns_total`, on a growing `spendgate_holds_in_flight` that does not fall
(stuck calls) and on a large `spendgate_catalog_age_seconds` (prices may be stale).

**Logs** are one JSON object per line on the `spendgate.call` logger, written for every settled
call and for events such as `sweep_failed`. Only an allowlist of fields can appear: `event`,
`request_id`, `key_prefix`, `scope_path`, `model`, `deployment`, `outcome`, token counts, `cost`,
`currency`, `latency_ms`, `ttft_ms`, `status`, `reason`, `level`, `ts`, for internal errors
`where`, `error_class`, `frames`, `charged`, and the budget-warning fields below (`budget_id`,
`scope_kind`, `period`, `period_start`, `spent`, `cap`). Prompts, response bodies, headers and secrets are
never logged: a field not on the list is dropped, and strings shaped like keys are dropped too.
Refused requests write a log line but no call row. `fail_open` opt-outs are logged as warnings at
start.

An unexpected exception (a bug in an adapter, the pipeline or an admin handler) that a catch-all
turns away writes one `spendgate.internal` event at level `ERROR`:

| Field | Meaning |
|---|---|
| `request_id` | The request (`sg_req_...`, or `sg_adm_...` for the admin API) |
| `where` | Which catch-all caught it (table below) |
| `error_class` | The exception's class name, e.g. `KeyError` |
| `frames` | The innermost 20 frames as `module:function:line`, outermost first, joined by ` > ` |
| `charged` | `true` when the call is charged (settled, not released) although it hit the bug |

| `where` | What the client sees | Money |
|---|---|---|
| `steps.prepare` | `400 invalid_request` | Nothing held |
| `pipeline.handle` | `500 internal_error` | Any hold is left to the sweeper; `charged` is `false` (not known) |
| `pipeline.forward` | `500 internal_error` | Full hold if the request was sent, else released |
| `routing.translate` | `500 internal_error` | Never sent: released |
| `routing.relay` | `500 internal_error` (stream not started) | Sent: the full hold |
| `routing.usage` | `500 internal_error` | Usage unreadable: the full hold |
| `routing.response` | `500 internal_error` | The provider answered: actual usage |
| `routing.error` | `500 internal_error` | Unknown what the provider did: the full hold |
| `stream.relay` | An in-stream `error` event after the `200` (then `[DONE]` for chat) | Usage seen so far, else the full hold |
| `stream.usage` | A normal end of the stream | Usage unreadable: the full hold |
| `admin.handler` | `500 internal_error` from the admin API | None |

The exception's message, local variables and source lines are never logged: a message can quote a
body or a secret. With `charged: true` the client got an error for a call it was charged for; the
`call` line with the same `request_id` carries the cost (when the ledger could not book the close,
the sweeper charges the hold later). `false` means released, refused or not known. In library mode
the same facts (never the message) go to the `spendgate.local` logger at level `ERROR`. Alert on
any `spendgate.internal` line and report it as a bug.

**Budget warnings** are WARNING lines on the `spendgate.budget` logger, in the same JSON format
and through the same allowlist. Each comes from the transaction that caused it, so a repeat call
or a retried settle never writes it again. How often each can appear:

| `event` | When |
|---|---|
| `soft_threshold` | An admitted hold takes `spent + held` to `soft_pct` of the effective cap or above (once per period, recorded as `soft_alerted_at`) |
| `cap_breached` | A settlement above its hold takes `spent + held` above the effective cap. Once per crossing: only the settlement that crosses writes it, and later overruns while above the cap do not. If `spent + held` drops back within the cap (in-flight holds settle low or are released) and a later overrun crosses again, that is a new crossing and a new line, so a period can have more than one |
| `blocked` | That breach flagged a `strict_overrun` budget for review: it refuses calls until `ack-overrun`. Written again only after an ack and a new breach |

Fields: `event`, `logger`, `budget_id`, `scope_path` (the scope that owns the budget), `period`
(`day`, `month` or `none`), `period_start` (RFC 3339 UTC; absent for `none`), `spent` (`spent +
held` at the transition), `cap` (the effective cap), both decimal strings, and `currency`. A
session budget is named by the key's scope plus `scope_kind: session`: a session path would carry
the key id and the caller's session id, so neither is logged. Nothing about the request, the key
or the user is included. A budget that was already above its cap (a lowered cap or an expired
grant) when an overrun is booked logs `blocked` without `cap_breached`.
In library mode the same records go to the `spendgate.budget` standard-library logger, with the
fields also in the message text.

The lines are notices, not the record. A line can be lost when a settlement commits but its reply
never reaches the gateway: the retry finds the reservation already settled, does nothing and
reports nothing. The ledger stays authoritative: `review_required_at` (`spendgate status`, the
admin API) for a block, `soft_alerted_at` for the threshold, and the metrics above.
