"""The base install must work without optional extras (T-SUP-06's companion)."""

import subprocess
import sys

OPTIONAL = ("psycopg", "psycopg_pool", "tiktoken")


def test_sqlite_ledger_and_core_never_import_optional_extras() -> None:
    # A fresh interpreter, so modules imported by other tests don't hide a stray import.
    code = (
        "import sys\n"
        "import spendgate.core.catalog, spendgate.core.cost, spendgate.core.estimate\n"
        "import spendgate.ledger.sqlite, spendgate.ledger.keys, spendgate.providers.port\n"
        f"print(sorted(m for m in sys.modules if m.split('.')[0] in {OPTIONAL!r}))\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter and code, no user input
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"
