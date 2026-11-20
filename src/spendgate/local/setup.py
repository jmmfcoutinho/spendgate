"""Ledger bootstrap for library mode: the default file, the `org:local` scope, its budget.

Everything here is idempotent, because several scripts (processes) share one ledger file and any
of them may be the first. Each create step has a fixed identity (scope path, key id, budget id),
so a second first-starter gets `ConflictError` (or finds the row) and carries on with what the
winner made; nothing can be created twice, so the cap cannot silently double.
"""

import contextlib
import os
import sys
from pathlib import Path

from spendgate.core.budgets import Period, ScopeKind
from spendgate.core.money import Money
from spendgate.ledger.port import ConflictError, LedgerPort
from spendgate.local.principal import LOCAL_KEY_ID

SCOPE_NAME = "local"
SCOPE_PATH = f"{ScopeKind.ORG.value}:{SCOPE_NAME}"
BUDGET_ID = "local-budget"  # fixed, so two first starters cannot both create a budget


class CapMismatch(ValueError):
    """The shared ledger already has a cap that differs from the one asked for."""


class NoBudget(ValueError):
    """Nothing limits spending on this ledger yet; library mode refuses to run unlimited."""


def default_ledger_path() -> Path:
    """`<user data dir>/spendgate/local-ledger.db` (XDG or Library/); POSIX only."""
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return base / "spendgate" / "local-ledger.db"


def prepare_directory(path: Path) -> None:
    """Create the ledger's folder private to this user (the file itself is 0600, T-LED-02)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)


async def prepare_scope(ledger: LedgerPort, ledger_path: Path, cap: Money | None, now: int) -> str:
    """The local scope's id, creating scope, key row and budget on first use.

    `cap=None` only reuses what exists and refuses a ledger with no budget at all (fail closed).
    A different cap on an existing budget is an error, not an overwrite: the file is a shared
    pool, and a script must not quietly raise a limit that another script relies on.
    """
    scope = await ledger.find_scope(SCOPE_PATH)
    if scope is None:
        try:
            scope = await ledger.create_scope(ScopeKind.ORG, SCOPE_NAME, None, now=now)
        except ConflictError:  # another process created it between our look and our write
            scope = await ledger.find_scope(SCOPE_PATH)
    if scope is None:  # unreachable: it exists, or creating it would not have conflicted
        raise NoBudget(f"{ledger_path}: cannot create or find scope {SCOPE_PATH}")
    scope_id = scope.id
    await ledger.add_key_record(LOCAL_KEY_ID, scope_id, now=now)  # idempotent
    rows = await ledger.totals(scope_id, now)
    if not rows and cap is not None:
        with contextlib.suppress(ConflictError):  # the winner's budget stands; compared below
            await ledger.create_budget(
                scope_id, Period.NONE, cap.nano, now=now, actor="local", budget_id=BUDGET_ID
            )
        rows = await ledger.totals(scope_id, now)
    if cap is None:
        if not rows:
            raise NoBudget(
                f"{ledger_path} has no budget; pass cap='5 USD' (or use spendgate.local.guard)"
            )
    elif all(r.effective_cap_nano != cap.nano for r in rows):
        have = ", ".join(Money(r.effective_cap_nano, cap.currency).format() for r in rows)
        raise CapMismatch(
            f"{ledger_path} already has a cap of {have}, not {cap.format()}; the cap is "
            "shared by every script on this ledger. Use another ledger_path for a new pool."
        )
    return scope_id
