"""The money state machine is guarded by explicit errors, never by `assert` (gone under `-O`)."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "spendgate"


@pytest.mark.parametrize("module", ["ledger/hold_flow.py", "pipeline/settle.py"])
def test_sr_14_state_machine_invariants_are_not_guarded_by_assert(module: str) -> None:
    """SR-14: no `assert` statement in the hold and settle flows."""
    tree = ast.parse((SRC / module).read_text(encoding="utf-8"))
    assert not [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assert)]
