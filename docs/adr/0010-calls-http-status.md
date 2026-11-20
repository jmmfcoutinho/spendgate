# ADR-0010: `calls.http_status` is the provider's status, range-checked (migration 0004)

**Status:** Accepted
**Date:** 2026-11-18

## Context

The first live smoke test needed reasoning to tell a provider 401 from a 404, because the call
record seemed to lack the provider's HTTP status. The `calls.http_status` column has existed since
`0001_init` and the pipeline fills it for every attempt, but it was never written down as a
contract, was not range-checked, was missing from `CallRow` and was absent from the call log.

## Decision

- `calls.http_status` is the HTTP status the provider returned for that attempt (for a stream, the
  opened response, normally 200). It is NULL when the request was never sent or no response
  arrived: connect failure, timeout before headers, cancel before the answer, a sweeper expiry.
  No second column is added.
- Migration `0004_calls_http_status_check` makes the range part of the schema: NULL or 100-599.
  PostgreSQL adds a named CHECK constraint. SQLite cannot add a CHECK to an existing table without
  rebuilding the audit table, so two triggers (`BEFORE INSERT`, `BEFORE UPDATE OF http_status`)
  abort an out-of-range write with the same message.
- `CallRow.http_status` is read by `get_calls`; the admin `Call` schema documents the field; the
  one-line call log carries `http_status` (the status of the last attempt) through the allowlist.

## Consequences

- Public behaviour: ledger schema (constraint), call log field, OpenAPI description. Older
  spendgate versions refuse a ledger migrated by 0004, as for every migration.
- The status is the provider's, not spendgate's answer to the caller (a provider 404 is a 502 to
  the caller); spendgate's own code stays in `error_code`.
- Usage grouping by status is not added; it would widen the usage whitelist for no asked-for need.
