# ADR-0009: Behaviour changes from the v0.1 code and security reviews

**Status:** Accepted
**Date:** 2026-11-17

## Context

The independent code and security reviews of v0.1 (`docs/reviews/code-review-v0.1.md`,
`docs/reviews/security-review-v0.1.md`) found defects whose fixes change public behaviour.

## Decision

- **Key before body (SR-01, SR-02).** The data plane checks the key from the headers before reading
  any body byte. JSON bodies have their own cap, `server.max_json_body_bytes` (default 4 MiB);
  transcription uploads keep `server.max_body_bytes`. A body that stalls or trickles is refused
  with HTTP 408 after `server.body_read_timeout_seconds` (default 30).
- **Request time limit (SR-13).** A request or stream runs at most `server.max_request_seconds`
  (default 900). Then it is answered 504 (buffered) or ended as `stream_broken`, settled at the
  full hold unless usage was seen.
- **Quota exhaustion (CR-06).** When every attempt ends on a provider quota or billing error, the
  answer is 502 `upstream_unavailable` without retry hints (it was a retryable 429).
- **No cost header unless booked (CR-05).** `X-Spendgate-Cost` is sent only when the ledger booked
  the settlement.
- **Readiness detail (SR-06).** `/readyz` on the data port returns only the status. Reasons and
  warnings (ledger, kill switch, clock skew, stale catalog) are at `GET /admin/v1/readiness`, which
  needs `usage:read`.
- **Bootstrap keys (SR-08).** `spendgate serve` creates bootstrap scopes and budgets but no keys;
  `spendgate migrate --bootstrap` creates keys and prints each new secret once, so secrets never
  reach server logs.
- **Empty model list (SR-09).** `allowed_models: []` is refused (`invalid_request`); `null` or an
  absent list means every model.
- **Lease renewal (CR-02).** The heartbeat renews only holds the process still owns; a hold whose
  settle failed is retried, then left to the sweeper.

## Consequences

Clients see 401 for a missing or bad key even on oversized bodies, 408 for stalled uploads, and
502 (not 429) for provider quota exhaustion. Operators create bootstrap keys with
`migrate --bootstrap`. Configuration gains three `server` settings; `docs/configuration.md` lists
them.

## Addendum (verification review)

- **Upload memory per key (NF-01).** Request bodies are charged to the gate as their bytes
  arrive (a declared Content-Length is charged at once), and one key may hold at most a quarter of
  the gate's memory, always at least one request. With the default 384 MiB budget a key can have
  one maximal 25 MB audio upload in flight at a time; a second concurrent one gets 429.
- **Streams nobody reads (NF-02).** The heartbeat enforces a stream's deadline and aborts; such a
  stream is settled (full hold unless usage was seen) even if the client stops reading.
- **Lost hold commits (NF-03).** After an ambiguous hold failure the gateway releases the hold
  (state-guarded, retried by the heartbeat), so a call never sent ends free.
