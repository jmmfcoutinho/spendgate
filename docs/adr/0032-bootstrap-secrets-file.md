# ADR-0032: `migrate --bootstrap` hands key secrets only to a terminal or a 0600 file

**Status:** Accepted
**Date:** 2026-11-27

## Context

Only a hash of each key is stored, so the run that creates a key is the only time its secret can
be shown. ADR-0009 (SR-08) moved bootstrap key creation out of `serve`, whose output is the
container log, into `spendgate migrate --bootstrap`, which prints each new secret once to stderr
(threat model C-17, T-LOG-04).

That still assumed a person at a terminal. `migrate --bootstrap` is also the natural command for
an unattended step: a Kubernetes Job or init container, a CI deploy job, a Compose service that
migrates before the guard starts. There stderr is collected, and each secret, admin keys included,
lands in log storage, kept and readable by more people than the key's owner. CodeQL flagged the
print (`py/clear-text-logging-sensitive-data`); the alert was dismissed on the promise of this
change.

Refusing to print without a terminal is not enough on its own: the key would be created anyway and
its secret lost, and the next run would not recreate it (bootstrap is create-only by name).

## Decision

- **A secret is printed only to a terminal.** When stderr is a terminal (`isatty()`),
  `migrate --bootstrap` prints `bootstrap: created <kind> key <name> (<scope>): <secret>` once, as
  before.
- **`--secrets-file PATH` writes them to a new file instead.** The file is opened with
  `O_CREAT | O_EXCL` and mode 0600 (a umask can only narrow it), so an existing file, or a symlink
  at that path, is never written through: the run refuses with exit code 2 before any bootstrap row
  is written. Each new key is one line of JSON, `{"name", "kind", "scope", "secret"}`, written and
  synced as soon as the key exists, so a failure later in the run does not lose a secret already
  issued. Stderr gets each key's name, kind and scope without the secret, then
  `bootstrap: wrote N key secrets to PATH`. A run that creates no key does not create the file.
  The flag wins over a terminal. It needs `--bootstrap`.
- **Without either, a run that would create a key refuses before writing anything.** Bootstrap
  first lists the configured keys that do not exist yet (by name and kind within their scope,
  revoked keys included, exactly the rule that decides creation); it writes nothing. If any would
  be created, stderr is not a terminal and no `--secrets-file` was given, the command exits 2
  (bad command line, as a refused overwrite in `catalog import` does) naming the keys and
  `--secrets-file`, and creates no scope, budget or key. The ledger migration itself has already
  run; it is idempotent.
- **Runs that create no key need neither.** A bootstrap with only scopes and budgets, or one whose
  keys all exist, runs unattended as before. So a Job can run `migrate --bootstrap` on every
  deploy: the first run needs `--secrets-file`, later runs do not.
- **Every key is checked before any row is written.** A configuration error in one key (an empty
  `models` list) used to surface only when bootstrap reached it, after earlier keys were created.
  It is now refused before the first scope.
- **`serve` is unchanged**: it creates scopes and budgets, never keys, and prints no secret.

JSON lines rather than one document, so a reader picks one key without parsing the rest
(`jq -r 'select(.name == "ops") | .secret' PATH`) and a partly written run is still readable line by
line.

## Consequences

- **Upgrade note.** A non-interactive `migrate --bootstrap` (no terminal on stderr) whose
  configuration declares a key that does not exist yet now exits 2 instead of printing the secret
  into the log. Add `--secrets-file PATH` on a volume only the operator reads, move the file's
  contents into your secret store and delete it, or create the keys once from a terminal.
  `docker compose run` gives the container a terminal when it is started from one, so the
  documented one-off still prints.
- The secrets file holds live keys in clear. It is the operator's to protect and delete; the mode
  only keeps other local users out. Inside a container it must be on a mounted volume to outlive
  the run.
- A failure between creating a key and syncing its line (a full disk) still loses that secret; the
  key exists and can be rotated or revoked (`spendgate keys rotate|revoke --local`).
- No change to the configuration schema, the ledger, the admin API or error codes. One new CLI
  flag; the exit code is the existing 2.
- T-LOG-04 now covers both commands: `serve` never prints or creates key secrets, and
  `migrate --bootstrap` hands each secret only to a terminal or a 0600 file, refusing otherwise.
