"""Numbered plain-SQL migrations, one folder per dialect (`NNNN_name.sql`, versions 1, 2, 3...)."""

import hashlib
import re
from dataclasses import dataclass
from importlib.resources import files

_NAME = re.compile(r"(\d{4})_([a-z0-9_]+)\.sql")


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str  # sha256 of the file; a changed applied migration is refused at start


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
        found.append(Migration(int(match[1]), match[2], text, digest))
    found.sort(key=lambda m: m.version)
    if [m.version for m in found] != list(range(1, len(found) + 1)):
        raise ValueError(f"{dialect} migrations must be numbered 1..n without gaps")
    return found
