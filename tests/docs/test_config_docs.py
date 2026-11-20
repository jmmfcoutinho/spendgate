"""docs/configuration.md is exactly what scripts/gen_config_docs.py generates from the schema."""

import importlib.util
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[2]


def generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "gen_config_docs", REPO / "scripts/gen_config_docs.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_reference_matches_the_generator() -> None:
    expected = generator().generate()
    committed = (REPO / "docs" / "configuration.md").read_text()
    assert committed == expected, "run: uv run python scripts/gen_config_docs.py"


def test_every_schema_key_is_documented() -> None:
    gen = generator()
    text = (REPO / "docs" / "configuration.md").read_text()
    for model, _ in gen._collect():
        for name in model.model_fields:
            assert f"| `{name}` |" in text, f"{model.__name__}.{name}"
