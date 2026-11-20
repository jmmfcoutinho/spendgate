# ADR-0005: SQLite and Postgres, no Redis in v0.1

**Status:** Proposed
**Date:** 2026-11-13

## Context

The ledger needs atomic reservations across concurrent calls and across processes (8 processes in the overshoot test). Single-host users want zero setup. Shared deployments need one database several gateways can use. Rate limits and counters could use Redis, but base setup must have no background services.

## Decision

Support two ledger backends behind one port: SQLite (default, single host, WAL mode) and Postgres (shared, optional extra). Use plain SQL, numbered migration files and no ORM. Reservations are atomic conditional updates inside a transaction, so no row-lock service is needed. Short-lived limit state (RPM, TPM, concurrency, cooldowns) is held in process memory in v0.1 and is per gateway process. No Redis.

## Decision drivers

- Two-minute local start with no server.
- Hard budgets that hold across processes.
- Fewer moving parts and dependencies.

## Alternatives considered

1. Postgres only.
2. SQLite plus Redis for limits and counters.
3. An ORM with a generic backend layer.

## Rationale

SQLite covers laptops and a single container. Postgres covers shared use with the same SQL. Money is always in the ledger, so only soft limits are per process. Redis is added only if Postgres proves too slow (see the [load review](../reviews/load-v0.1.md)).

## Consequences

### Positive

- One SQL dialect subset to test on both backends.
- No extra services in the base setup.

### Negative

- With several gateway processes, RPM and TPM limits are per process, not global. Budgets stay exact.
- SQLite writes serialise, which caps throughput on one host.

### Risks and follow-up

- Run the same ledger test suite on both backends.
- Measure Postgres hold and settle latency before accepting no Redis.

## Related documents

- Requirements: embedded storage for local use; no overshoot
- Architecture: architecture.md section 2
- Supersedes: none
