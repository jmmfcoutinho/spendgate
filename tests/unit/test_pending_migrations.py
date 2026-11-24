"""`pending_migrations` and `recorded`: the pure half both SQL backends share (ADR-0018).

The backends read `schema_migrations` and the packaged files; these functions alone decide what
runs and what is refused, so they are checked here without a database.
"""

import pytest

from spendgate.ledger.migrations import Applied, Migration, pending_migrations, recorded
from spendgate.ledger.port import LedgerUnavailable

PACKAGED = [Migration(1, "init", "SELECT 1", "c1"), Migration(2, "more", "SELECT 2", "c2")]


def test_a_new_ledger_runs_every_packaged_migration_in_order() -> None:
    assert pending_migrations({}, PACKAGED) == PACKAGED


def test_a_current_ledger_runs_nothing() -> None:
    applied = {1: Applied("c1", False), 2: Applied("c2", False)}
    assert pending_migrations(applied, PACKAGED) == []


def test_only_the_missing_versions_run() -> None:
    assert pending_migrations({1: Applied("c1", False)}, PACKAGED) == PACKAGED[1:]


def test_a_changed_applied_migration_is_refused() -> None:
    with pytest.raises(LedgerUnavailable, match="migration 1 changed"):
        pending_migrations({1: Applied("other", False)}, PACKAGED)


def test_one_compatible_version_ahead_is_accepted_and_anything_else_refused() -> None:
    current = {1: Applied("c1", False), 2: Applied("c2", False)}
    assert pending_migrations(current | {3: Applied("c3", True)}, PACKAGED) == []
    with pytest.raises(LedgerUnavailable, match="newer spendgate"):
        pending_migrations(current | {3: Applied("c3", False)}, PACKAGED)
    with pytest.raises(LedgerUnavailable, match="newer spendgate"):
        pending_migrations({1: Applied("c1", False), 3: Applied("c3", True)}, PACKAGED)


def test_recorded_reads_rows_with_and_without_the_compatible_flag() -> None:
    rows = [
        {"version": 1, "checksum": "c1"},  # a table made before ADR-0018 has no flag
        {"version": 2, "checksum": "c2", "compatible": 1},
    ]
    assert recorded(rows) == {1: Applied("c1", False), 2: Applied("c2", True)}
