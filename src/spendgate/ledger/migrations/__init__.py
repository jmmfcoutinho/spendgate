"""Numbered plain-SQL migrations, one folder per dialect (`NNNN_name.sql`, versions 1, 2, 3...).

Expand/contract (ADR-0018): a migration whose file carries the line
`-- compatible_with_previous: true` only adds (a nullable column, a table, an index, a
`NOT VALID` constraint), so the previous release still works on the schema it leaves. The flag
is stored with the applied row, which is how that older code knows it may start one version
ahead. Every other migration is a contract step and refuses older code.
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files

from spendgate.ledger.port import LedgerUnavailable

_NAME = re.compile(r"(\d{4})_([a-z0-9_]+)\.sql")
_COMPATIBLE = re.compile(r"^-- compatible_with_previous: (true|false)$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str  # sha256 of the file; a changed applied migration is refused at start
    compatible: bool = False  # expand step: the previous release still runs on the result


@dataclass(frozen=True, slots=True)
class Applied:
    """One `schema_migrations` row, as the code that applied it recorded it."""

    checksum: str
    compatible: bool


class SchemaBehind(LedgerUnavailable):
    """The ledger needs migrations this process was told not to apply."""


def compatible_marker(text: str) -> bool:
    match = _COMPATIBLE.search(text)
    return match is not None and match[1] == "true"


def load(dialect: str) -> list[Migration]:
    """The packaged migrations of `dialect`, in order. Gaps or odd file names are errors."""
    found: list[Migration] = []
    for entry in files(__package__).joinpath(dialect).iterdir():
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        match = _NAME.fullmatch(entry.name)
        if match is None:
            raise ValueError(f"migration file name must be NNNN_name.sql: {entry.name}")
        text = entry.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode()).hexdigest()
        found.append(Migration(int(match[1]), match[2], text, digest, compatible_marker(text)))
    found.sort(key=lambda m: m.version)
    if [m.version for m in found] != list(range(1, len(found) + 1)):
        raise ValueError(f"{dialect} migrations must be numbered 1..n without gaps")
    return found


def pending(packaged: list[Migration], applied: Mapping[int, Applied]) -> list[Migration]:
    """The packaged migrations not applied yet, in order.

    Refuses a changed applied migration, and a schema from a newer release unless it is exactly
    one version ahead and that version was recorded as compatible with the previous release.
    """
    known = {m.version: m for m in packaged}
    for version, row in applied.items():
        if version in known and row.checksum != known[version].checksum:
            raise LedgerUnavailable(f"migration {version} changed after it was applied")
    todo = [m for m in packaged if m.version not in applied]
    ahead = sorted(set(applied) - set(known))
    if ahead:
        latest = len(packaged)
        if todo or ahead != [latest + 1] or not applied[latest + 1].compatible:
            raise LedgerUnavailable(
                f"the ledger was migrated by a newer spendgate (schema version {max(ahead)}, "
                f"this version knows {latest}); refusing to start"
            )
    return todo


def require_current(todo: list[Migration], applied: Mapping[int, Applied]) -> None:
    """Refuse to start on a schema that is behind: migrating is a separate, explicit step."""
    if todo:
        have = max(applied, default=0)
        raise SchemaBehind(
            f"the ledger schema is at version {have} and this spendgate needs "
            f"{todo[-1].version}: run `spendgate migrate --config ...` first "
            "(or start with `serve --migrate`, or set ledger.auto_migrate: true)"
        )
