# ADR-0011: Refuse a `model_filter` that matches no route; late settle updates the reservation

**Status:** Accepted
**Date:** 2026-11-22

## Context

A review found two gaps (issue #19):

- A budget's `model_filter` is a case-sensitive glob matched against the catalog id
  (`openai/gpt-6-luna`), never the alias a client sends. The docs said only "matching models",
  and neither the admin API nor the configuration checked the value. A filter written against an
  alias stored a budget that looked configured but never took part in a hold.
- A settle that arrives after the sweeper expired the hold books `max(0, charge - held)` as
  overrun and rewrites the sweeper's `calls` row to `max(held, charge)` (M-04). The reservation
  row kept `charged_nano` at the swept hold, as `ledger-state-machine.md` specified, so
  `get_reservation` showed less than the budgets and the call row carried.

## Decision

- The admin API refuses `POST /admin/v1/scopes/{scope_id}/budgets` when `model_filter` matches
  no routed catalog id, with the existing `invalid_request` code (400) and
  `param: model_filter`. No new error code.
- For `bootstrap.budgets`, `spendgate check-config` and `spendgate serve` print a warning per
  filter that matches no routed catalog id. It stays a warning: the configuration is otherwise
  valid, and bootstrap budgets are create-only.
- The match target is documented in the configuration reference, the OpenAPI document, the
  operations guide and the state machine, and implemented once (`core.policy.model_filter_matches`).
- On a late settle the reservation's `charged_nano` is set to `max(held, charge)` in the same
  transaction, the same value as the sweeper's rewritten `calls` row. Its state stays `expired`.

## Consequences

- Public behaviour: an API client that created budgets with never-matching filters now gets a
  400 instead of a stored budget that never applied. `get_reservation` after a late settle with
  an overrun now shows the hold plus the overrun.
- No schema change and no data migration. Existing budget rows with such filters and existing
  late-settled reservations are left unchanged.
- Money booked to the budgets is unchanged; only validation and reporting change.
