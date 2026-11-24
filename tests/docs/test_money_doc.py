"""`docs/how-money-is-protected.md` cites real tests, covers every money rule and stays short."""

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "how-money-is-protected.md"
TEXT = DOC.read_text()
# A full node id, or a `::name` shorthand that continues the previous file in the text.
CITATION = re.compile(r"(tests/[\w/]+\.py)?((?:::\w+)+)")


def cited() -> list[tuple[str, tuple[str, ...]]]:
    found: list[tuple[str, tuple[str, ...]]] = []
    current = ""
    for match in CITATION.finditer(TEXT):
        current = match.group(1) or current
        assert current, f"shorthand {match.group(0)} before any test file"
        found.append((current, tuple(match.group(2).split("::")[1:])))
    return found


def defined(path: str, names: tuple[str, ...]) -> bool:
    tree = ast.parse((REPO / path).read_text())
    first, *rest = names
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == first:
            return not rest
        if isinstance(node, ast.ClassDef) and node.name == first:
            methods = {
                n.name for n in node.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
            }
            return len(rest) == 1 and rest[0] in methods
        # Hypothesis builds its test class by assignment: `TestX = Machine.TestCase`.
        if isinstance(node, ast.Assign) and first in {
            t.id for t in node.targets if isinstance(t, ast.Name)
        }:
            return rest == ["runTest"]
    return False


def test_every_cited_test_exists() -> None:
    citations = cited()
    assert len(citations) > 40
    missing = [f"{p}::{'::'.join(n)}" for p, n in citations if not defined(p, n)]
    assert not missing, f"cited tests that do not exist: {missing}"


def test_every_money_rule_has_a_row_with_a_test() -> None:
    for number in range(1, 16):
        rule = f"M-{number:02d}"
        row = next((ln for ln in TEXT.splitlines() if ln.startswith(f"| {rule} |")), None)
        assert row is not None, f"{rule} is missing from the table"
        assert "::test_" in row or "::Test" in row, f"{rule} names no test"


def test_the_page_names_at_most_five_source_files() -> None:
    sources = set(re.findall(r"src/spendgate/[\w/]+\.py", TEXT))
    assert 0 < len(sources) <= 5, sorted(sources)
    for source in sources:
        assert (REPO / source).is_file(), source
