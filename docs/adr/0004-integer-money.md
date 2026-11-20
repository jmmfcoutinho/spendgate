# ADR-0004: Integer money

**Status:** Proposed
**Date:** 2026-11-13

## Context

Token prices are tiny (fractions of a cent per thousand tokens). Floats drift, and drift in a ledger breaks audits and budget comparisons. Prices come in USD while the ledger currency is EUR by default (decision D4).

## Decision

All money is an integer number of nano-units of the ledger currency (1 EUR = 1,000,000,000 nano-EUR). Rounding is always ceiling, so the guard never under-charges. Catalog prices are stored as integers per unit (for example nano-units per million tokens). FX rates are decimal strings, applied once, and the rate used is recorded on each call. No floats appear in `core.money`, the ledger or the catalog. Database columns are 64-bit integers.

## Decision drivers

- Exact sums and comparisons in budgets.
- Atomic SQL updates with no decimal types.
- Same arithmetic on SQLite and Postgres.

## Alternatives considered

1. Python `Decimal` and database `NUMERIC`.
2. Floats with a tolerance.
3. Integer cents (too coarse for cheap tokens).

## Rationale

Integers are exact, portable and fast. Nano-units give enough resolution for the cheapest models. 64-bit range holds about 9.2 billion EUR, which is far above any budget.

## Consequences

### Positive

- No rounding surprises. Easy property tests.
- Ceiling rounding gives a conservative bound.

### Negative

- Values need formatting for humans in CLI and API.
- Many small calls round up slightly, so recorded spend can exceed true cost by under one nano-unit per call.

### Risks and follow-up

- Multiplication of large token counts by price must be done in Python integers and checked against the 64-bit limit before storing.

## Related documents

- Requirements: decisions.md D4
- Architecture: architecture.md (`core.money`)
- Supersedes: none
