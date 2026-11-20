# ADR-0003: Fail closed

**Status:** Proposed
**Date:** 2026-11-13

## Context

A spend guard that lets calls through when it is unsure defeats its purpose. LiteLLM fails open by default in several paths (unpriced models, budget errors). Our users run agents that can loop for hours.

## Decision

When the guard cannot prove a call is allowed, it refuses before any upstream contact. This covers: unknown or unpriced model (`model_not_priced`), unknown deployment, unreachable or erroring ledger (`ledger_unavailable`), unreadable config, failed authentication, and oversize requests (`request_too_large`). A stream that breaks is charged in full. Every fail-open option lives only in the `fail_open:` section of `spendgate.yaml`, is off by default and is logged at start. Catalog data can never fail open (no zero-charge-on-missing-usage option; `free_statuses` is limited to 429 and 529).

## Decision drivers

- A hard budget must stay hard when parts fail.
- Predictable behaviour that auditors can verify with block tests.

## Alternatives considered

1. Fail open with an alert (LiteLLM default).
2. Fail open for reads, closed for writes.
3. Per-provider choice with no global default.

## Rationale

A short outage is cheaper than an unbounded bill. Refusals are visible and retryable; overspend is not reversible.

## Consequences

### Positive

- The guarantee fits in one sentence.
- Block tests in CI prove it.

### Negative

- Ledger outage stops all LLM traffic through the gateway.
- Missing price entries block new models until the catalog is updated.

### Risks and follow-up

- Provide a clear error body and a documented, explicit opt-in for fail-open in low-risk setups.

## Related documents

- Requirements: unpriced models are refused; no overshoot
- Architecture: architecture.md section 3
- Supersedes: none
