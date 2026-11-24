# Operations

Day-two tasks for a running proxy: backups, keys, the kill switch, usage, retention, the sweeper
and reading metrics and logs. Setup is in [Running locally](deploy/local.md); every setting is in
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
([admin API](admin-openapi.yaml)).

## Retention

Call records older than `ledger.retention_days` (default 90) are deleted by a job that `serve`
runs once a day, starting at its first pass. Whole UTC days are kept or deleted together, finished
reservations and sessions go with them, and pending holds never do. Consequences:

- Usage reports cover the retention window only; a `since` before it is refused rather than
  silently truncated.
- Budget totals do not depend on call rows, so deleting old calls never changes a balance.
- Deletion is permanent. Export what you need to keep (`usage --json`) or back up first, before
  lowering the setting.

## The sweeper

A hold whose owner died (a crash, a killed container) would block budget forever, so a background
sweeper in `serve` (and in library mode) charges and releases holds whose lease expired. It runs
every `ledger.sweep_seconds` (default 30). An expired hold is charged **in full**, because a call
that may have reached the provider is paid for. After a crash, a budget therefore shows held money
for up to `ledger.lease_seconds` (default 60) plus one sweep interval, then it settles. Nothing to schedule: every instance sweeps, any of them may settle an expired hold, and a hold
still being renewed by a live instance is left alone. A failing pass is logged as `sweep_failed` and retried on the next tick, never fatal.

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
`currency`, `latency_ms`, `ttft_ms`, `status`, `reason`, `level`, `ts`, and for internal errors
`where`, `error_class`, `frames`, `charged`. Prompts, response bodies, headers and secrets are
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
