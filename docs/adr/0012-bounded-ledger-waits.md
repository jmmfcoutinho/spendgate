# ADR-0012: Bounded ledger waits (Postgres session limits, hold deadline)

**Status:** Accepted
**Date:** 2026-11-22

## Context

A review found that nothing bounded how long a call could wait on the ledger. Pooled Postgres
connections had no `lock_timeout`, `statement_timeout`, `idle_in_transaction_session_timeout` or
TCP keepalives, and the hold transaction runs before the request deadline (`max_request_seconds`)
starts. A budget row locked by another session (an open `psql` transaction, a long admin change)
held a call for 40 s and more, with the caller's connection and gate place kept for the whole
wait.

## Decision

- Every pooled Postgres connection gets `lock_timeout` 2s, `statement_timeout` 5s and
  `idle_in_transaction_session_timeout` 10s from the pool's `configure` hook, set only where
  `pg_settings` reports the built-in default. A value from the DSN `options`, `PGOPTIONS`,
  `ALTER ROLE/DATABASE ... SET` or `postgresql.conf` wins. `lock_timeout` stays above the default
  `deadlock_timeout` (1 s), so deadlocks are still reported as such.
- TCP options (`keepalives=1`, `keepalives_idle=15`, `keepalives_interval=5`,
  `keepalives_count=3`, `tcp_user_timeout=30000`) are added as libpq options unless the DSN names
  them. The user timeout stops a `COMMIT` to a silent server from waiting through the kernel's
  ~15 minutes of TCP retransmission.
- Usage reports, call and audit lists and retention batches run with `SET LOCAL
  statement_timeout = 60s`, but only where the 5 s default above is in force; an operator's own
  `statement_timeout` is kept for them too.
- Lease renewal selects the rows it renews with `FOR NO KEY UPDATE SKIP LOCKED` (a
  `$SKIP_LOCKED` token in the shared SQL; nothing on SQLite, which serialises writers anyway).
  Without this, renewing every owned hold in one transaction would wait on a row whose settle
  waits on budget rows, hit `lock_timeout`, and stop every open stream as `renewal_failed`. A row
  is locked by its owner's own close (an open stream is unregistered before it closes, so this is
  never a watched stream) or by a sweeper expiring a lease that is already overdue.
- The pool's waiting queue is not capped. A cap refuses every pool caller, not only new holds: in
  an ordinary burst a settle or a renewal would be refused at once, stopping open streams and
  leaving live holds to be charged in full by the sweeper. New holds are already bounded by the
  10 s deadline below, every caller by the pool timeout (10 s), and a hold that commits after its
  caller was refused is given back.
- A lock timeout (55P03) or a cancelled statement (57014) is not retried: it is
  `ledger_unavailable`. Deadlocks (40P01) and serialization failures (40001) are still retried, up
  to 3 times.
- The pipeline waits at most `HOLD_SECONDS` (10 s) for the hold, on every backend, then refuses
  with `ledger_unavailable` and makes no upstream contact. The hold task is not cancelled; whatever
  it commits later is released at 0 by the existing give-back, as after a client cancel.
- The migration connection at start gets keepalives only: a migration may wait for another
  instance's migration lock or run long.

## Consequences

- Default policy changes: a call that used to wait on a locked budget row is now refused as
  `ledger_unavailable` (503, `Retry-After: 5`) after about 2 s, and any hold slower than 10 s is
  refused. This is fail closed (ADR-0003): no provider call is made and nothing is charged.
- Operators who need longer waits raise them through DSN `options` (for example
  `options=-c lock_timeout=5s`) or role, database or server settings; the hold deadline and the
  60 s report limit are internal and not configurable.
- A long lock on one hold's row no longer fails the heartbeat: renewal skips that row and renews
  the rest. An open stream's row is skipped only while a sweeper works on its overdue lease, and
  the stream is then stopped as `lease_lost`, as before this change. If that sweep rolls back
  (for example 55P03 on a budget row), the hold stays pending and a later sweep expires it; the
  stopped stream is settled once by its owner either way. Reporting "locked, still pending"
  separately so the stream could continue was left out: it would change the `renew_all` port
  contract for a case that needs an overdue lease and a failed sweep.
- No new config keys, error codes, endpoints or dependencies. Documented in
  [operations.md](../operations.md#postgres-connections).
