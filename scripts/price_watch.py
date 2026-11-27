"""Regenerate the importer-generated catalog blocks and summarise the change (issue #109).

A maintainer tool run by `.github/workflows/price-watch.yml` and `make price-watch`, never by the
proxy (ADR-0014, ADR-0028 point 6). It reads snapshots the importers downloaded, so it needs no
key and makes no request except LiteLLM's file when `--litellm-commit` is given:

    uv run python scripts/bedrock_prices.py --download SNAP/aws
    uv run python scripts/azure_prices.py --download SNAP/azure.json
    uv run python scripts/vertex_prices.py --download SNAP/vertex     # needs the Pricing API key
    uv run python scripts/price_watch.py --from-dir SNAP --out OUT [--litellm-commit <sha>]

Each importer runs `--check` first and `--write` only when its block is out of date, so a day
without a price change leaves the catalog byte for byte as it was (`--write` would otherwise move
`checked_on`). Any other exit of an importer stops the run with exit 2 and nothing is proposed: a
source format change fails loudly instead of yielding partial data. The LiteLLM diff runs on the
regenerated catalog and is informational only.

Writes into OUT: `title.txt`, `body.md` (the pull request text), `commit.txt` (the commit
message) and, with `--litellm-commit`, `litellm.txt` (the full diff report).
Exit codes: 0 done, changed or not; 2 an importer or git failed.
"""

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "src" / "spendgate" / "catalog" / "prices.yaml"
MAX_IDS = 40  # per list in the pull request body, which GitHub caps at 65,536 characters
CAVEAT = "Review against the providers' pages; merge only after checking."


@dataclass(frozen=True)
class Importer:
    script: str
    source: str  # where its prices come from, for the summary
    snapshot: str  # the importer's downloaded data, relative to the snapshot directory
    flag: str  # how the importer is told where that data is


IMPORTERS = (
    Importer("bedrock_prices.py", "AWS Price List", "aws", "--from-dir"),
    Importer("azure_prices.py", "Azure Retail Prices API", "azure.json", "--from-file"),
    Importer("vertex_prices.py", "Google Cloud Pricing API", "vertex", "--from-dir"),
)


@dataclass
class Change:
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.added or self.changed or self.removed)


@dataclass(frozen=True)
class LiteLLM:
    commit: str
    exit_code: int
    summary: str  # the report's SUMMARY lines, or "" when there is no report


class WatchError(Exception):
    """An importer or git failed: nothing may be proposed."""


Run = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # stderr streams to the log, so an importer's own error message is what a red run shows.
    return subprocess.run(  # noqa: S603 - fixed argv built here, no shell
        list(argv), cwd=ROOT, stdout=subprocess.PIPE, text=True, check=False
    )


def entries_by_id(text: str) -> dict[str, list[str]]:
    """Each id's entries, canonical and without `checked_on`, so a re-check date is no change."""
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    doc = yaml.load(text, Loader=loader)  # noqa: S506 - a safe loader
    found: dict[str, list[str]] = {}
    for entry in doc.get("models") or []:
        rest = {k: v for k, v in entry.items() if k != "checked_on"}
        found.setdefault(str(entry["id"]), []).append(json.dumps(rest, sort_keys=True, default=str))
    return {k: sorted(v) for k, v in found.items()}


def compare(before: str, after: str) -> Change:
    old, new = entries_by_id(before), entries_by_id(after)
    return Change(
        added=sorted(new.keys() - old.keys()),
        changed=sorted(i for i in old.keys() & new.keys() if old[i] != new[i]),
        removed=sorted(old.keys() - new.keys()),
    )


def regenerate(snapshot: Path, catalog: Path, run: Run) -> dict[str, Change]:
    """Run every importer's `--check`, and its `--write` on drift; the ids each one changed."""
    changes: dict[str, Change] = {}
    for imp in IMPORTERS:
        base = [sys.executable, f"scripts/{imp.script}", imp.flag, str(snapshot / imp.snapshot)]
        base += ["--catalog", str(catalog)]
        check = run([*base, "--check"])
        if check.returncode not in (0, 1):
            raise WatchError(f"{imp.script} --check exited {check.returncode}")
        before = catalog.read_text(encoding="utf-8")
        if check.returncode == 1:
            write = run([*base, "--write"])
            if write.returncode != 0:
                raise WatchError(f"{imp.script} --write exited {write.returncode}")
        changes[imp.script] = compare(before, catalog.read_text(encoding="utf-8"))
    return changes


def litellm_summary(report: str) -> str:
    """The SUMMARY block of a `litellm_prices.py diff` report."""
    lines = report.splitlines()
    if "SUMMARY" not in lines:
        return ""
    block = lines[lines.index("SUMMARY") + 1 :]
    end = block.index("") if "" in block else len(block)
    return "\n".join(line.strip() for line in block[:end])


def cross_check(commit: str, catalog: Path, out: Path, run: Run) -> LiteLLM:
    report = out / "litellm.txt"
    argv = [sys.executable, "scripts/litellm_prices.py", "diff", "--commit", commit]
    done = run([*argv, "--catalog", str(catalog), "--out", str(report)])
    text = report.read_text(encoding="utf-8") if report.exists() else ""
    return LiteLLM(commit, done.returncode, litellm_summary(text))


def _ids(label: str, ids: Sequence[str]) -> list[str]:
    if not ids:
        return []
    shown = ", ".join(f"`{i}`" for i in ids[:MAX_IDS])
    more = f", and {len(ids) - MAX_IDS} more" if len(ids) > MAX_IDS else ""
    return [f"- {label} ({len(ids)}): {shown}{more}"]


def _count(change: Change) -> str:
    if not change:
        return "unchanged"
    parts = [(len(change.added), "added"), (len(change.changed), "changed")]
    parts.append((len(change.removed), "removed"))
    return ", ".join(f"{n} {what}" for n, what in parts if n)


def _litellm_lines(litellm: LiteLLM | None) -> list[str]:
    if litellm is None:
        return ["Not run (no `--litellm-commit`)."]
    verdict = {
        0: "no failing finding",
        1: "some prices or dates disagree with LiteLLM: read the report before merging",
    }.get(litellm.exit_code, f"the diff could not run (exit {litellm.exit_code})")
    lines = [f"Against BerriAI/litellm `{litellm.commit}`: {verdict}."]
    if litellm.summary:
        lines += ["", "```text", litellm.summary, "```"]
    return [*lines, "", "The full report is the `price-watch` artifact of the workflow run."]


def render_body(changes: Mapping[str, Change], stat: str, litellm: LiteLLM | None) -> str:
    """The pull request description: what each importer changed, the diff stat, LiteLLM."""
    lines = [
        "## What and why",
        "",
        "The daily price watch (`.github/workflows/price-watch.yml`, ADR-0028 point 6) ran the "
        "catalog importers on fresh provider data and their generated blocks of "
        "`src/spendgate/catalog/prices.yaml` changed. Nothing is merged automatically, and the "
        "proxy still fetches no prices at runtime.",
        "",
        f"**{CAVEAT}**",
        "",
        "| Importer | Source | Ids |",
        "|---|---|---|",
    ]
    sources = {imp.script: imp.source for imp in IMPORTERS}
    for script, change in changes.items():
        lines.append(f"| `scripts/{script}` | {sources.get(script, '-')} | {_count(change)} |")
    for script, change in changes.items():
        if change:
            lines += ["", f"### `scripts/{script}`", ""]
            lines += _ids("added", change.added) + _ids("changed", change.changed)
            lines += _ids("removed", change.removed)
    lines += ["", "### Diff stat", "", "```text", stat.rstrip() or "(no diff)", "```"]
    lines += ["", "### LiteLLM cross-check (informational)", "", *_litellm_lines(litellm)]
    lines += [
        "",
        "## Before merging",
        "",
        "- [ ] Every changed price compared with the provider's own page",
        "- [ ] `CI gate` green on the head commit",
        "- [ ] Squash-merged, then shipped in a patch release (`docs/releasing.md`)",
    ]
    return "\n".join(lines) + "\n"


def render_commit(title: str, changes: Mapping[str, Change]) -> str:
    lines = [title, ""]
    lines += [f"{script}: {_count(change)}" for script, change in changes.items()]
    return "\n".join([*lines, "", CAVEAT]) + "\n"


def diff_stat(catalog: Path, run: Run) -> str:
    done = run(["git", "diff", "--stat", "--", str(catalog)])
    if done.returncode != 0:
        raise WatchError(f"git diff exited {done.returncode}")
    return done.stdout


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("--from-dir", type=Path, required=True, help="the importers' snapshots")
    p.add_argument("--out", type=Path, required=True, help="where to write the proposal")
    p.add_argument("--litellm-commit", help="also diff against LiteLLM at this commit")
    p.add_argument("--catalog", type=Path, default=CATALOG, help=argparse.SUPPRESS)
    p.add_argument("--today", default=None, help="the date in the title (default: today, UTC)")
    return p


def main(argv: Sequence[str] | None = None, run: Run = run_command) -> int:
    args = _parser().parse_args(argv)
    today: str = args.today or datetime.now(UTC).date().isoformat()
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        changes = regenerate(args.from_dir, args.catalog, run)
        stat = diff_stat(args.catalog, run)
    except WatchError as exc:
        print(f"price_watch: {exc}; nothing is proposed", file=sys.stderr)
        return 2
    litellm: LiteLLM | None = None
    if args.litellm_commit:
        litellm = cross_check(args.litellm_commit, args.catalog, args.out, run)
    title = f"prices: daily price watch {today}"
    files = {
        "title.txt": title + "\n",
        "body.md": render_body(changes, stat, litellm),
        "commit.txt": render_commit(title, changes),
    }
    for name, text in files.items():
        (args.out / name).write_text(text, encoding="utf-8")
    for script, change in changes.items():
        print(f"{script}: {_count(change)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
