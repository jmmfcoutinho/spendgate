"""scripts/price_watch.py: check before write, fail loudly, summarise per importer (#109).

No network and no importer runs: a fake runner stands in for the importers, git and LiteLLM.
"""

import importlib.util
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash") or "/bin/bash"
CATALOG = """\
catalog_version: 1
models:
  - id: bedrock/model-a
    effective_from: "2026-11-01"
    meters: {input_per_mtok: "1", output_per_mtok: "2"}
    checked_on: "2026-11-01"
  - id: azure_openai/global.model-b
    effective_from: "2026-11-01"
    meters: {input_per_mtok: "3", output_per_mtok: "4"}
    checked_on: "2026-11-01"
"""
REPORT = """\
LiteLLM BerriAI/litellm @ abc

SUMMARY
  our entries in force: 2
  in both, different:   1 (plus 0 accepted, ours higher)
  failing findings:     1

PRICES THAT DIFFER (per meter; ours vs LiteLLM)
"""


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("price_watch", REPO / "scripts/price_watch.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["price_watch"] = module
    spec.loader.exec_module(module)
    return module


pw: Any = load_script()


class FakeRun:
    """Importers by script name: the `--check` exit code, and what `--write` does to the file."""

    def __init__(self, catalog: Path, checks: dict[str, int], writes: dict[str, str]) -> None:
        self.catalog, self.checks, self.writes = catalog, checks, writes
        self.calls: list[list[str]] = []
        self.write_exit = 0
        self.litellm_exit = 1

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        self.calls.append(args)
        if args[0] == "git":
            return subprocess.CompletedProcess(args, 0, " prices.yaml | 2 +-\n")
        script = Path(args[1]).name
        if script == "litellm_prices.py":
            Path(args[args.index("--out") + 1]).write_text(REPORT, encoding="utf-8")
            return subprocess.CompletedProcess(args, self.litellm_exit, "")
        if "--check" in args:
            return subprocess.CompletedProcess(args, self.checks.get(script, 0), "a diff\n")
        old, new = self.writes[script].split("->")
        text = self.catalog.read_text(encoding="utf-8")
        self.catalog.write_text(text.replace(old, new), encoding="utf-8")
        return subprocess.CompletedProcess(args, self.write_exit, "")


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    path = tmp_path / "prices.yaml"
    path.write_text(CATALOG, encoding="utf-8")
    return path


def watch(catalog: Path, run: FakeRun, *extra: str) -> int:
    argv = ["--from-dir", str(catalog.parent / "snap"), "--out", str(catalog.parent / "out")]
    result: int = pw.main([*argv, "--catalog", str(catalog), "--today", "2026-11-27", *extra], run)
    return result


def test_a_new_checked_on_date_alone_is_no_price_change() -> None:
    later = CATALOG.replace('checked_on: "2026-11-01"', 'checked_on: "2026-11-27"')
    assert not pw.compare(CATALOG, later)


def test_added_changed_and_removed_ids_are_told_apart() -> None:
    after = CATALOG.replace('input_per_mtok: "1"', 'input_per_mtok: "1.5"').replace(
        "azure_openai/global.model-b", "azure_openai/global.model-c"
    )
    change = pw.compare(CATALOG, after)
    assert change.added == ["azure_openai/global.model-c"]
    assert change.changed == ["bedrock/model-a"]
    assert change.removed == ["azure_openai/global.model-b"]


def test_an_importer_in_date_is_checked_and_never_written(catalog: Path) -> None:
    run = FakeRun(catalog, checks={}, writes={})
    assert watch(catalog, run) == 0
    assert catalog.read_text(encoding="utf-8") == CATALOG
    assert not [c for c in run.calls if "--write" in c]
    checked = [Path(c[1]).name for c in run.calls if "--check" in c]
    assert checked == ["bedrock_prices.py", "azure_prices.py", "vertex_prices.py"]


def test_drift_is_written_and_summarised_per_importer(catalog: Path) -> None:
    run = FakeRun(
        catalog,
        checks={"bedrock_prices.py": 1},
        writes={"bedrock_prices.py": 'input_per_mtok: "1"->input_per_mtok: "1.25"'},
    )
    assert watch(catalog, run, "--litellm-commit", "a" * 40) == 0
    out = catalog.parent / "out"
    body = (out / "body.md").read_text(encoding="utf-8")
    assert "| `scripts/bedrock_prices.py` | AWS Price List | 1 changed |" in body
    assert "| `scripts/azure_prices.py` | Azure Retail Prices API | unchanged |" in body
    assert "- changed (1): `bedrock/model-a`" in body
    assert "prices.yaml | 2 +-" in body
    assert pw.CAVEAT in body
    assert "in both, different:   1" in body
    assert "disagree with LiteLLM" in body
    title = (out / "title.txt").read_text(encoding="utf-8")
    assert title == "prices: daily price watch 2026-11-27\n"
    commit = (out / "commit.txt").read_text(encoding="utf-8")
    assert commit.startswith(title + "\nbedrock_prices.py: 1 changed\n")


@pytest.mark.parametrize("stage", ["check", "write"])
def test_an_importer_failure_stops_the_run_and_proposes_nothing(
    catalog: Path, stage: str, capsys: pytest.CaptureFixture[str]
) -> None:
    run = FakeRun(
        catalog,
        checks={"azure_prices.py": 2 if stage == "check" else 1},
        writes={"azure_prices.py": "model-b->model-b2"},
    )
    run.write_exit = 2
    assert watch(catalog, run) == 2
    assert "nothing is proposed" in capsys.readouterr().err
    assert not (catalog.parent / "out" / "body.md").exists()
    assert not [c for c in run.calls if "vertex_prices.py" in c[1]]  # stops at the first failure


def test_the_litellm_diff_is_informational(catalog: Path) -> None:
    run = FakeRun(catalog, checks={}, writes={})
    run.litellm_exit = 2  # could not download: still exit 0, and the body says so
    assert watch(catalog, run, "--litellm-commit", "b" * 40) == 0
    body = (catalog.parent / "out" / "body.md").read_text(encoding="utf-8")
    assert "the diff could not run (exit 2)" in body


def test_long_id_lists_are_cut_to_keep_the_body_small() -> None:
    ids = [f"azure_openai/regional.m{i:03}" for i in range(pw.MAX_IDS + 5)]
    body = pw.render_body({"azure_prices.py": pw.Change(added=ids)}, "", None)
    assert f"- added ({pw.MAX_IDS + 5}): " in body
    assert ", and 5 more" in body
    assert ids[-1] not in body


def test_litellm_summary_is_the_summary_block_only() -> None:
    assert pw.litellm_summary(REPORT).splitlines() == [
        "our entries in force: 2",
        "in both, different:   1 (plus 0 accepted, ours higher)",
        "failing findings:     1",
    ]
    assert pw.litellm_summary("no summary here\n") == ""


def test_the_pull_request_text_and_commit_message_pass_the_ci_message_check() -> None:
    change = pw.Change(added=["bedrock/x"], changed=["bedrock/y"], removed=["bedrock/z"])
    litellm = pw.LiteLLM("c" * 40, 1, "failing findings: 1")
    texts = [
        pw.render_body({"bedrock_prices.py": change}, " prices.yaml | 9 +++--\n", litellm),
        pw.render_commit("prices: daily price watch 2026-11-27", {"bedrock_prices.py": change}),
    ]
    for text in texts:
        run = subprocess.run(  # noqa: S603 - fixed script, test-controlled input on stdin
            [BASH, str(REPO / "scripts/check-commit-message.sh"), "-"],
            input=text,
            capture_output=True,
            text=True,
            check=False,
        )
        assert run.returncode == 0, run.stderr
