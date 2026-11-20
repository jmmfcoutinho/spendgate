# ADR-0008: Scope display name (migration 0003)

**Status:** Accepted
**Date:** 2026-11-17

## Context

The admin API creates and renames scopes. A scope's identity is its `path`
(`org:acme/project:app`), built from segment values that are restricted to a safe character set
and never change, because budgets, keys, kill switches and the audit log refer to scopes by id and
path. Operators also want a readable label ("Acme Corp", "Nightly import") for dashboards and
lists, and that label must be editable without touching the identity. The first schema
(`0001_init`) had no place for one.

## Decision

Add a nullable free-text column `scopes.display_name` in migration `0003_scope_display_name`
(identical for SQLite and PostgreSQL):

```sql
ALTER TABLE scopes ADD COLUMN display_name TEXT;
```

- Optional on `ScopeCreate`; changed through `ScopeUpdate` (`update_scope`), which writes a
  `scope.update` audit row with the before and after values.
- Display only. It is not part of `path`, not unique, not used in any lookup, budget, limit,
  kill-switch or routing decision, and never appears in logs (the log allowlist does not include it).
- NULL means "none"; readers fall back to the path.
- The migration is append-only and additive: a ledger migrated by 0003 is refused by an older
  spendgate ("migrated by a newer spendgate"), as for every migration; older rows read NULL.

## Rationale

Keeping the label out of `path` preserves the rule that identity never changes, so renaming costs
nothing and invalidates nothing. A plain nullable column needs no backfill and no table rewrite on
either backend. A separate table or a JSON `labels` column would add structure nobody asked for.

## Consequences

- Public behaviour changes: the admin API schema gains `display_name`; the ledger schema gains a
  column (hence this ADR, per the change-management rule).
- `ScopeRow` carries `display_name`; contract tests cover create, rename and audit on both backends.
- No uniqueness rule; the API caps the label at 120 characters; a hostile label is data, never
  interpreted.
