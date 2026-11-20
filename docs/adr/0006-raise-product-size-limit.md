# ADR-0006: Raise the product size limit to 8,000 lines

**Status:** Superseded by [ADR-0007](0007-drop-the-total-size-limit.md)
**Date:** 2026-11-13

## Context

The project set a hard limit of ~5,000 product lines (Python under `src/spendgate`, excluding
`providers/`), checked by `scripts/lightweight_report.py` in CI. The design estimates were
5,500–6,000 after cuts. With the pure core and both ledgers done, the count is 4,817:
`core/` 2,147 and `ledger/` 1,838 (SQLite and Postgres drivers, keys, 19 money scenarios' worth of
flows), with no module over 400 lines and no function over 50. The pipeline, gateway, config, SDK
and CLI still need an estimated 2,500–3,500 lines.

## Decision

Raise the hard product-line limit to **8,000**. The per-module (400), per-function (50),
per-adapter (400) and runtime-dependency (6) limits are unchanged. After the gateway lands, run a
trimming pass and aim to finish below the limit.

## Decision drivers

- Keep the features that protect money: audio measured from bytes, a Postgres ledger for shared
  budgets, the admin API.
- Stay small enough to audit: the money path (`core/` + `ledger/`) stays around 4,000 lines.
- A limit that is exceeded by design teaches people to ignore the gate.

## Alternatives considered

1. Keep 5,000 and cut features (audio parsing, the Postgres ledger, parts of the admin API).
   Rejected: each cut weakens a guarantee or a target user.
2. Turn the limit into a warning until the gateway lands. Rejected: a soft gate drifts.

## Consequences

- `lightweight_report.py` defaults to 8,000; README, CONTRIBUTING and the design summary
  state 8,000.
- For scale: LiteLLM's `proxy_server.py` alone is ~23,000 lines. Raising the limit again needs a new ADR.
