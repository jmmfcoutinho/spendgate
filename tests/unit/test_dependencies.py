"""T-SUP-06: the base install stays at six runtime dependencies or fewer."""

import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).parents[2] / "pyproject.toml"
MAX_RUNTIME_DEPENDENCIES = 6


def count_runtime_dependencies(pyproject_text: str) -> int:
    return len(tomllib.loads(pyproject_text).get("project", {}).get("dependencies", []))


def test_base_install_has_at_most_six_runtime_dependencies() -> None:
    assert count_runtime_dependencies(PYPROJECT.read_text("utf-8")) <= MAX_RUNTIME_DEPENDENCIES


@pytest.mark.parametrize(("deps", "ok"), [(6, True), (7, False)])
def test_counter_flags_a_seventh_dependency(deps: int, ok: bool) -> None:
    listed = ", ".join(f'"dep{i}"' for i in range(deps))
    text = f"[project]\ndependencies = [{listed}]\n"
    assert (count_runtime_dependencies(text) <= MAX_RUNTIME_DEPENDENCIES) is ok
