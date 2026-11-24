# ADR-0022: `calls.error_code` holds stable, documented codes

**Status:** Accepted
**Date:** 2026-11-24

## Context

`calls.error_code` said why an attempt failed, but its values were free strings from four places:
stop reasons spelled inline (`lease_expiring`, `max_duration`, `client_gone`), gateway faults
(`relay_error`, `adapter_error`), protocol `ErrorCode` values for provider answers, and, for
transport and stream failures, the Python class name of the exception (`ReadTimeout`,
`ConnectError`, `StreamLimitExceeded`). Class names belong to the HTTP client library and can
change in any upgrade; nothing listed the values, so the column could not be relied on by the
admin API's readers or by a usage query (issue #37).

## Decision

- `core.failures` defines two `StrEnum`s: `StopReason` (why the gateway stopped a started stream)
  and `AttemptError` (why an attempt failed on the wire or inside the gateway). The type
  `CallErrorCode = StopReason | AttemptError | ErrorCode | Outcome` is the only type
  `AttemptRecord.error_code` accepts, so `mypy --strict` rejects a free string.
- The transport maps each httpx exception class to an `AttemptError` (`connect_error`,
  `read_timeout`, `write_error`, ...), most specific first; any other HTTP client error is
  `transport_error` and a non-HTTP exception `unexpected_error`. A broken stream is
  `stream_limit_exceeded` (a crossed C-22 limit) or `stream_malformed`.
- data-model.md lists every value. A test keeps that list, the enums and the httpx mapping in
  step; a new httpx exception class fails it until it is mapped.
- Existing rows keep their values. The old class names are listed there as legacy values.

## Consequences

- Public behaviour: new `calls` rows (and the admin API's `error_code` field) carry the new
  spellings for transport and stream failures, e.g. `read_timeout` instead of `ReadTimeout`.
  Stop reasons, `relay_error`, `adapter_error`, provider codes and interrupted outcomes are
  written as before. A report over a window that spans the upgrade sees both spellings.
- No migration and no CHECK constraint: rows keep legacy values, and a constraint would have to
  allow them forever. Money is unaffected: the outcome, not the code, decides the charge.
- Adding a code means adding an enum member and its data-model.md row; renaming one is a breaking
  change and needs a new ADR.
