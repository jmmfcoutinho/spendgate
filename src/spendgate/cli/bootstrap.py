"""`migrate --bootstrap`: hand each new key's secret over once, to a terminal or to a 0600 file.

A secret printed to a stream that is not a terminal (a Kubernetes Job, CI, a Compose service whose
output is collected) lands in log storage (C-17). So secrets go to stderr only when it is a
terminal, or to `--secrets-file`; with neither, the bootstrap refuses before it writes any row, so
no key exists whose secret nobody saw (ADR-0032). A run that creates no key needs neither.
"""

import json
import os
import sys

from spendgate.cli.client import EXIT_CONFIG, EXIT_USAGE, CliError
from spendgate.cli.runtime import Runtime
from spendgate.gateway.bootstrap import (
    BootstrapReport,
    CreatedKey,
    apply_bootstrap,
    keys_to_create,
)

NOTHING_CREATED = "no bootstrap row was created"


def print_bootstrap(report: BootstrapReport, *, secrets_file: str | None = None) -> None:
    """Created rows on stderr (stdout may be a pipe). Each new key's secret is printed once,
    unless it went to `secrets_file`."""
    out = sys.stderr
    for path in report.scopes:
        print(f"bootstrap: created scope {path}", file=out)
    for budget in report.budgets:
        print(f"bootstrap: created budget {budget}", file=out)
    for key in report.keys:
        line = f"bootstrap: created {key.kind} key {key.name} ({key.scope})"
        print(line if secrets_file is not None else f"{line}: {key.secret}", file=out)
    if secrets_file is not None:
        print(f"bootstrap: wrote {len(report.keys)} key secrets to {secrets_file}", file=out)
    for warning in report.warnings:
        print(f"warning: bootstrap: {warning}", file=out)


class SecretsFile:
    """A new file at `path`, mode 0600, never one that exists (nor a symlink): one JSON object per
    key, synced as each key is created. Removed again if no key was written to it."""

    def __init__(self, path: str) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags, 0o600)  # the umask can only narrow 0600
        except FileExistsError:
            message = f"--secrets-file {path} exists; refusing to overwrite it"
            raise CliError(EXIT_USAGE, f"{message}; {NOTHING_CREATED}") from None
        except OSError as exc:
            message = f"cannot create --secrets-file {path} ({type(exc).__name__})"
            raise CliError(EXIT_CONFIG, f"{message}; {NOTHING_CREATED}") from None
        self.path, self.written = path, 0
        self._file = os.fdopen(fd, "w", encoding="utf-8")

    def write(self, key: CreatedKey) -> None:
        entry = {"name": key.name, "kind": key.kind, "scope": key.scope, "secret": key.secret}
        self._file.write(json.dumps(entry) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())
        self.written += 1

    def close(self) -> None:
        self._file.close()
        if self.written == 0:  # an empty file would only block the next run's O_EXCL
            os.unlink(self.path)


async def migrate_bootstrap(rt: Runtime, *, secrets_file: str | None, interactive: bool) -> None:
    """`interactive`: stderr is a terminal, so a printed secret reaches a person, not a log."""
    pending = await keys_to_create(rt.settings, rt.ledger)
    if pending and secrets_file is not None:
        sink = SecretsFile(secrets_file)  # before any row: an existing file refuses the run
        try:
            report = await apply_bootstrap(
                rt.settings, rt.ledger, rt.keys, rt.now(), on_key=sink.write
            )
        finally:
            sink.close()
        print_bootstrap(report, secrets_file=secrets_file)
        return
    if pending and not interactive:
        raise CliError(
            EXIT_USAGE,
            f"bootstrap would create keys {', '.join(pending)}, but stderr is not a terminal, so "
            f"their secrets would land in a log; pass --secrets-file PATH (created 0600) or run "
            f"it in a terminal; {NOTHING_CREATED}",
        )
    print_bootstrap(await apply_bootstrap(rt.settings, rt.ledger, rt.keys, rt.now()))
    if secrets_file is not None:
        print(f"bootstrap: no key to create; {secrets_file} not written", file=sys.stderr)
