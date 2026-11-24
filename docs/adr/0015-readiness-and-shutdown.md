# ADR-0015: A kill switch leaves readiness alone; keep-alive and drain deadline are settings

**Status:** Accepted
**Date:** 2026-11-22

## Context

Issue #32 found three gaps in how `serve` behaves behind a load balancer or an orchestrator:

- `/readyz` returned 503 with the reason `kill_switch_engaged` while a global kill switch was on.
  The switch lives in the shared ledger, so every replica turned unready at the same moment and
  the load balancer dropped all targets. Clients then got the balancer's own error instead of the
  documented `kill_switch` refusal, and lifting the switch waited on the balancer's healthy
  threshold.
- uvicorn ran with its defaults: an idle keep-alive of 5 s, below the 60 s idle timeout of many
  load balancers, so a balancer could reuse a connection the proxy had just closed (a 502); and
  no drain deadline, so the orchestrator's kill ended every drain that ran long, leaving the
  holds still open to the sweeper.
- The operations guide had no statement of what a ledger failover looks like to clients.

## Decision

- Readiness reflects only what makes one replica unable to serve: the ledger answers and the
  catalog is loaded. A kill switch of any kind never fails it. An engaged global switch is
  reported as the warning `kill_switch_engaged: ...` on `GET /admin/v1/readiness`, and a failed
  read of the switch as the warning `kill_switch_probe_failed`; the data-port `/readyz` still
  answers with the status alone (SR-06). Refusals stay in the pipeline:
  `kill_switch` (503) with the JSON error body, and running streams end within one heartbeat.
- Two new `server` keys, passed to uvicorn for the data listener and the admin listener:
  - `keep_alive_seconds` (default 75): uvicorn's `timeout_keep_alive`. 75 s is above the common
    60 s load balancer idle timeout, so the balancer closes an idle connection first.
  - `graceful_shutdown_seconds` (default 30): uvicorn's `timeout_graceful_shutdown`. After
    `SIGTERM` the listener closes and calls in flight run until this deadline; then uvicorn
    cancels the rest and the pipeline settles each one as a client cancel (M-13), before the
    lifespan shutdown drains the settles. The deadline is meant to end before the orchestrator's
    grace period (minus any `preStop` wait), so the process settles its own calls instead of the
    sweeper charging them in full after a kill. Set it to at least `max_request_seconds` to let
    every call finish. The client of a stopped call gets uvicorn's plain 500 (or, for a stream
    under way, a cut connection), and uvicorn logs one error per stopped call.
  - With a separate admin listener, the data server passes each signal on to the admin server,
    so both drain in parallel under the same deadline.
- `deploy/compose/compose.yaml` sets `stop_grace_period: 50s`: Docker's 10 s default would kill
  the process in the middle of a 30 s drain.
- The operations guide states the observable behaviour of a ledger outage or failover per
  backend: calls are refused with `ledger_unavailable` until the ledger answers again, then
  recover with no restart. It gives no recovery number: that is the database's.

## Consequences

- Public behaviour: with a global kill switch engaged, `/readyz` answers 200 instead of 503, and
  the admin readiness detail lists `kill_switch_engaged` under `warnings` instead of `reasons`.
  An operator who used `/readyz` to watch for the switch reads the admin detail or
  `spendgate status` instead.
- Before this change a drain had no deadline of its own. With the default of 30 s, a call or
  stream still running 30 s after `SIGTERM` is now stopped and charged as cancelled (the full
  hold once sent, unless usage was seen), and its client gets a plain 500 or a cut stream instead
  of the answer. Before, it ran on until the orchestrator killed the process, and was then
  charged in full by the sweeper. Operators who let long streams finish
  raise `graceful_shutdown_seconds` and the grace period together.
- Idle connections stay open 75 s instead of 5 s: a few more idle sockets per instance.
- No schema change, no new error code, no new dependency.
