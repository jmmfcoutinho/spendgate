# ADR-0007: Drop the total product-size limit

**Status:** Accepted (maintainer, 2026-11-17)
**Date:** 2026-11-17
**Supersedes:** [ADR-0006](0006-raise-product-size-limit.md)

## Context

ADR-0006 raised the hard limit on product lines (Python under `src/spendgate`, excluding
`providers/`) from 5,000 to 8,000. After the pipeline landed the count was 8,407: ledger 3,063
(SQLite, Postgres, keys, usage), core 2,147, pipeline 1,625, config 572, gateway 571, client 426.
Streaming, the admin API and CLI, and library mode were still to come (an estimated 1,100–1,500
more lines). Every module and function was within its own limit.

## Decision

Remove the hard limit on total product lines (a maintainer decision). The size report still
prints the total for information. These limits stay hard and gate CI: modules and provider
adapter files at most 400 lines, functions at most 50, at most 6 base runtime dependencies.

## Rationale

The per-file and per-function limits are what keep the code readable and auditable; a total
cap was forcing a choice between features that protect money and a number.

## Consequences

- `scripts/lightweight_report.py` no longer fails on the total.
- README, CONTRIBUTING and the design summary no longer promise a total.
- A trimming pass before release still looks for code that can go.
