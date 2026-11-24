# ADR-0018: Migrations are a separate step; one compatible step ahead is accepted

**Status:** Accepted
**Date:** 2026-11-23

## Context

`serve` applied pending migrations every time it opened the ledger, on both backends. On a shared
Postgres ledger that turns every rolling restart into a schema change made by whichever instance
starts first, with no backup point and no operator in the loop. The reverse direction was closed
too: code refused any schema with a version it did not know, so after an upgrade had migrated the
database, the previous release could not start, and a rollback meant a restore even when the new
migration only added a nullable column.

## Decision

- `spendgate migrate` applies pending migrations. `serve` applies them only with `serve --migrate`
  or `ledger.auto_migrate: true`. Otherwise it checks the schema without writing anything and, on a
  schema that is behind, refuses to start: `error: the ledger schema is at version N and this
  spendgate needs M: run spendgate migrate ...`, exit code 5. The `--local` admin commands follow
  the same setting.
- `ledger.auto_migrate` is a new, optional config key. Unset, it is `true` for SQLite and `false`
  for Postgres. A SQLite ledger is one file on one host, so no older process can be running on it
  while this one starts, and library mode (SQLite only) keeps migrating on open; changing that
  default would only add a step to the single-host setup. A Postgres ledger is shared, so its
  schema changes when the operator says so.
- Expand/contract. A migration file that carries the line `-- compatible_with_previous: true` is an
  expand step: it only adds, so the previous release still works on the schema it leaves. The flag
  is stored with the applied row in a new column, `schema_migrations.compatible` (added to an
  existing table by the next `migrate`; rows applied before it are contract steps). Code starts
  on a schema exactly one version ahead when that version is recorded as compatible; two versions
  ahead, or one contract step ahead, is still refused. Only the database can carry this: the older
  code cannot know about a migration written after it.
- Postgres commits each migration in its own transaction, each under the existing advisory
  migration lock and after re-reading what is applied. A `CHECK` added `NOT VALID` by one
  migration can then be validated (`VALIDATE CONSTRAINT`, which takes a weaker lock) by a later
  one, instead of scanning the table under the strong lock that adding the constraint holds until
  commit. A failure leaves the schema at the last version that committed; running `migrate` again
  continues from there. SQLite keeps one `BEGIN IMMEDIATE` transaction for all of them (one writer
  anyway, no `NOT VALID`).
- A lint test (`tests/unit/test_migration_lint.py`) checks every migration after 0004: a Postgres
  `ADD CONSTRAINT ... CHECK` needs `NOT VALID` and its `VALIDATE` in a later file; a migration
  marked compatible may not drop a table, view or column, rename, change a column type, set
  `NOT NULL`, delete or truncate rows, or add a `NOT NULL` column without a default. Both
  dialects must have the same versions, names and markers. Migrations 0001 to 0004 are never
  edited (their checksums are recorded in every ledger) and predate the rules.

## Consequences

- Public behaviour changes: on Postgres, starting a new release with `serve` on an old schema now
  refuses instead of migrating. The documented flow (README, `docs/deploy/local.md`,
  `deploy/compose`) runs `spendgate migrate` (in Compose, `docker compose run --rm spendgate
  migrate`) before `up`. Operators who want the old behaviour set `ledger.auto_migrate: true` or
  pass `--migrate`.
- A ledger last migrated by the previous release is accepted as it is when its versions are
  current, even without the new column; `migrate` adds the column, which that previous release
  ignores, so rolling back past this change still works.
- A rollback without a restore works for one release with at most one migration, marked
  compatible. A release that needs a contract step (drop, rename, tighten) ships it after the
  expand step it depends on has been out for a release, and its changelog says so; rolling it
  back means restoring the backup taken before `migrate`.
- A refused open is now printed as one `error:` line with exit code 5, not a traceback, for every
  ledger error at start.
- One new config key and one bookkeeping column. No new error codes, endpoints or dependencies.
  Runbook in [operations.md](../operations.md#upgrades-and-rollbacks).
