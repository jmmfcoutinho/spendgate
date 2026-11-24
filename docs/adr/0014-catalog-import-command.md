# ADR-0014: `spendgate catalog import` drafts an override file from LiteLLM's price file

**Status:** Accepted
**Date:** 2026-11-22

## Context

Issue #31. Operators who want more models than the bundled catalog carries had one route: the
maintainer script `scripts/litellm_prices.py override`, which is not shipped in the package. It
maps LiteLLM's community price file (`BerriAI/litellm`) onto catalog entries, but an installed
spendgate cannot run it, and hand-writing dozens of entries invites mistakes.

The catalog stays a local, reviewed file by design ([catalog-schema.md](../catalog-schema.md)):
a price fetched over the network while serving would let whoever controls that file, or the path
to it, set any price to 0 and switch the guard off.

## Decision

- New command: `spendgate catalog import (--from-litellm <sha> | --from-file <path>) --out
  <file> [--providers <slug,...>] [--force]`. It takes no `--config` and no admin key.
- `--from-litellm` takes a full 40-character lower-case commit SHA, never a branch or tag, so
  the bytes cannot move under the operator. The file is
  `https://raw.githubusercontent.com/BerriAI/litellm/<sha>/model_prices_and_context_window.json`:
  https only, that host only, no redirects followed, at most 32 MiB. `--from-file` reads a local
  copy instead and downloads nothing; with both flags the file is taken as a copy of that commit.
- The output is a new catalog override file: every entry `verified: false`, `effective_from` and
  `checked_on` today (UTC), `source_url` the pinned raw URL, and a header with the SHA-256 of the
  bytes read. Ids the installed bundled catalog carries when the file is drafted are left out, so
  at draft time no unverified entry supersedes a bundled price. A later release can bundle one of
  the drafted ids; then the drafted entry, dated later, wins from its date. `spendgate
  check-config` therefore warns whenever a `verified: false` entry supersedes a verified entry of
  the same id (a later `effective_from`, or the same one in a later file), and operators re-run
  the import or delete those entries after upgrading.
- Each LiteLLM entry is drafted or skipped on its own, with a reason on stderr that names the key
  (printed with escapes, never raw) and no value. Skipped: a value that is not a finite JSON
  number (NaN, Infinity, a string, a boolean, an exponent out of range), a price of 0 in any
  meter or a long-context multiplier below 1 (catalog data cannot fail open, M-10), a chat,
  responses or messages entry without `max_output_tokens` or with a fractional one (never a
  guessed or rounded ceiling, M-09), and anything else the catalog would not load. LiteLLM's 0
  output price on embeddings, which bill no output, is dropped rather than drafted. The file is
  loaded with the bundled catalog through `core.catalog.parse` before it is written.
- An existing `--out` is never overwritten without `--force` (checked before the download and
  again on an exclusive open). With `--force` the file is written beside `--out`, flushed to disk
  and renamed over it, so a failure leaves the old file whole. `--from-file` reads at most
  32 MiB plus one byte, so a pipe, a device or a growing file is refused rather than read without
  end.
- Exit codes: 0 written; 2 bad command line or the output exists; 3 the input is not LiteLLM's
  JSON, is too large, cannot be read, or yields nothing to write; 5 the download failed.
- The mapping moves into `spendgate.core.litellm` (pure: bytes and dicts in, dicts out), shared
  with `scripts/litellm_prices.py`. The download and file handling live in `spendgate.cli.catalog`.
  An import-linter contract ("only the catalog command uses the LiteLLM mapper") keeps both out
  of reach of the gateway, library mode, the pipeline and the leaf packages.

## Consequences

- Public behaviour: a new CLI command, and the first outbound request spendgate itself makes
  that is not a provider call. It happens only when an operator runs the command; `serve`,
  `check-config`, `migrate` and library mode still read local files only.
- No new runtime dependency (httpx and PyYAML are already base dependencies), no config key, no
  error code, no ledger or schema change. `check-config` gains one warning line (unverified over
  verified); it never fails the check.
- The generated entries are LiteLLM's numbers, unchecked. The start-up warning for unverified
  entries names them, and the header says so; the risk of a wrong price stays with the operator
  who loads the file.
- The maintainer script keeps its own pinned commit and file
  (`litellm/model_prices_and_context_window_backup.json`) for the weekly diff; only the mapping
  is shared.
