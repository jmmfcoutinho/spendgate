"""Every threat row needs a control and a test id, or an accepted-risk note; every cited test id
needs a test that carries it, or an explicit "no test yet" entry (SR-10)."""

import re
from collections.abc import Iterable
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
THREAT_MODEL = ROOT / "docs" / "threat-model.md"
TESTS = ROOT / "tests"
THREAT_ROW = re.compile(r"^\|\s*(TH-\d+)\s*\|")
CONTROL_ID = re.compile(r"\bC-\d+\b")
TEST_ID = re.compile(r"\bT-[A-Z]+-\d+\b")
ACCEPTED_RISK = re.compile(r"[Aa]ccepted risk R-\d+")
ID_RANGE = re.compile(r"\b(T-([A-Z]+)-(\d+)) to T-\2-(\d+)\b")
# In a test file an id is `T-AUTH-01` in a docstring or comment, or `t_auth_01` in a function name.
CARRIER = re.compile(r"(?<![A-Za-z0-9])[Tt][-_]([A-Za-z]+)[-_](\d+)")
NO_TEST_LINE = re.compile(r"^- (?:Planned|CI job)\b[^:]*:(.*)$", re.M)
EXPECTED_COLUMNS = 8  # ID, threat, STRIDE, L/I, control, test, residual (+ outer empty cells)


def check_threat_model(text: str) -> list[str]:
    """Return one problem per defective threat row; also fail if no rows are found."""
    problems: list[str] = []
    rows = 0
    for line in text.splitlines():
        match = THREAT_ROW.match(line)
        if not match:
            continue
        rows += 1
        threat_id = match.group(1)
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != EXPECTED_COLUMNS - 1:
            problems.append(f"{threat_id}: expected 7 columns, found {len(cells)}")
            continue
        control, proof = cells[4], cells[5]
        if not CONTROL_ID.search(control):
            problems.append(f"{threat_id}: no control id (C-NN)")
        if not (TEST_ID.search(proof) or ACCEPTED_RISK.search(proof)):
            problems.append(f"{threat_id}: no test id and no accepted-risk note")
    if rows == 0:
        problems.append("no threat rows found")
    return problems


def cited_test_ids(text: str) -> set[str]:
    """Ids named in the document's tables, with `T-X-04 to T-X-06` ranges expanded."""
    cited: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cited.update(TEST_ID.findall(line))
        for match in ID_RANGE.finditer(line):
            cited.update(f"T-{match[2]}-{n:02d}" for n in range(int(match[3]), int(match[4]) + 1))
    return cited


def listed_without_a_test(text: str) -> set[str]:
    return {i for m in NO_TEST_LINE.finditer(text) for i in TEST_ID.findall(m[1])}


def ids_carried_by(files: Iterable[Path]) -> set[str]:
    found: set[str] = set()
    for path in files:
        for match in CARRIER.finditer(path.read_text("utf-8")):
            found.add(f"T-{match[1].upper()}-{int(match[2]):02d}")
    return found


def uncovered_test_ids(text: str, files: Iterable[Path]) -> list[str]:
    """Cited ids that no test carries and the document does not admit to lacking."""
    return sorted(cited_test_ids(text) - ids_carried_by(files) - listed_without_a_test(text))


def test_every_cited_test_id_has_a_test_or_is_listed_as_not_yet_written() -> None:
    text = THREAT_MODEL.read_text("utf-8")
    assert len(cited_test_ids(text)) > 50  # the parser still sees the tables
    assert uncovered_test_ids(text, TESTS.rglob("*.py")) == []


def test_a_cited_id_with_no_test_and_no_listing_is_caught(tmp_path: Path) -> None:
    table = "| TH-99 | x | S | H/H | C-01 | T-AUTH-01, T-NOPE-07, T-GONE-01 to T-GONE-02 | r |\n"
    carrier = tmp_path / "test_x.py"
    carrier.write_text("def test_t_auth_01_something() -> None: ...\n")
    assert uncovered_test_ids(table, [carrier]) == ["T-GONE-01", "T-GONE-02", "T-NOPE-07"]
    listed = table + "- Planned (not written): T-NOPE-07, T-GONE-01, T-GONE-02\n"
    assert uncovered_test_ids(listed, [carrier]) == []
    assert uncovered_test_ids(table, [carrier]) != []
    comment = tmp_path / "test_y.py"
    comment.write_text('# T-NOPE-07\n"""T-GONE-01 and T-GONE-02"""\n')
    assert uncovered_test_ids(table, [carrier, comment]) == []


def test_every_threat_has_a_control_and_a_test_or_accepted_risk() -> None:
    assert check_threat_model(THREAT_MODEL.read_text("utf-8")) == []


@pytest.mark.parametrize(
    ("row", "fragment"),
    [
        ("| TH-99 | x | S | H/H | none | T-AUTH-01 | r |", "no control id"),
        ("| TH-99 | x | S | H/H | C-01 | see later | r |", "no test id"),
        ("| TH-99 | x | S | H/H | C-01 | | r |", "no test id"),
        ("| TH-99 | x | S | H/H | C-01 | T-AUTH-01 |", "columns"),
    ],
)
def test_doctored_rows_are_rejected(row: str, fragment: str) -> None:
    problems = check_threat_model(row)
    assert any(fragment in p for p in problems)


def test_accepted_risk_note_replaces_a_test_id() -> None:
    row = "| TH-99 | x | S | H/H | C-01 | Accepted risk R-01 | r |"
    assert check_threat_model(row) == []


def test_empty_document_is_rejected() -> None:
    assert check_threat_model("no table here") == ["no threat rows found"]


def test_doctoring_the_real_file_is_caught() -> None:
    text = THREAT_MODEL.read_text("utf-8")
    assert "| C-01 constant-time compare of hashes |" in text
    doctored = text.replace("| C-01 constant-time compare", "| constant-time compare")
    assert any("TH-17" in p for p in check_threat_model(doctored))
