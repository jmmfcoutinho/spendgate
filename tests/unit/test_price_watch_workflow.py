"""`.github/workflows/price-watch.yml` keeps its privileges small (ADR-0028 point 6, #109).

Static checks on the YAML: what can start it, what each job may do, where the Google key goes,
and that the branch and the CI it dispatches are ones `ci.yml` accepts.
"""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
TEXT = (WORKFLOWS / "price-watch.yml").read_text("utf-8")
FLOW: dict[Any, Any] = yaml.safe_load(TEXT)
CI: dict[Any, Any] = yaml.safe_load((WORKFLOWS / "ci.yml").read_text("utf-8"))
JOBS: dict[str, Any] = FLOW["jobs"]


def triggers(doc: dict[Any, Any]) -> dict[str, Any]:
    found = doc["on"] if "on" in doc else doc[True]  # PyYAML reads a bare `on` as True
    assert isinstance(found, dict)
    return found


def steps() -> list[tuple[str, dict[str, Any]]]:
    return [(name, step) for name, job in JOBS.items() for step in job["steps"]]


def test_it_starts_only_on_its_schedule_or_by_hand() -> None:
    assert set(triggers(FLOW)) == {"schedule", "workflow_dispatch"}
    assert not re.search(r"^\s*pull_request(_target)?:", TEXT, re.M)  # not even nested
    assert JOBS["regenerate"]["if"] == "github.ref == 'refs/heads/main'"


def test_permissions_are_read_only_except_where_a_job_needs_one_write() -> None:
    assert FLOW["permissions"] == {"contents": "read"}
    assert {name: job["permissions"] for name, job in JOBS.items()} == {
        "regenerate": {"contents": "read"},
        "propose": {"contents": "write", "pull-requests": "write"},
        "dispatch-ci": {"actions": "write"},
    }


def test_every_action_is_pinned_by_commit_sha() -> None:
    uses = [step["uses"] for _, step in steps() if "uses" in step]
    assert uses
    assert all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u) for u in uses), uses


def test_no_checkout_keeps_the_token_in_git_config() -> None:
    checkouts = [s for _, s in steps() if s.get("uses", "").startswith("actions/checkout@")]
    assert len(checkouts) == 2
    assert all(s["with"]["persist-credentials"] is False for s in checkouts)


def test_the_google_key_reaches_only_the_vertex_download_step() -> None:
    assert TEXT.count("secrets.") == 1
    holders = [(job, s) for job, s in steps() if "secrets." in str(s)]
    assert len(holders) == 1
    job, step = holders[0]
    assert job == "regenerate"
    assert step["env"] == {"GOOGLE_PRICING_API_KEY": "${{ secrets.GOOGLE_PRICING_API_KEY }}"}
    assert "scripts/vertex_prices.py --download" in step["run"]
    assert "GOOGLE_PRICING_API_KEY" not in step["run"]  # never echoed or passed on the command line
    assert "GOOGLE" not in str(FLOW["env"]) and "env" not in JOBS["regenerate"]


def test_only_the_job_that_reads_the_google_key_uses_the_main_only_environment() -> None:
    # The key is a secret of the `price-watch` environment, restricted to `main` in the repository
    # settings, so only this job, on main, receives it.
    assert JOBS["regenerate"]["environment"] == "price-watch"
    assert all("environment" not in job for name, job in JOBS.items() if name != "regenerate")


def test_the_jobs_that_write_run_no_python() -> None:
    for name in ("propose", "dispatch-ci"):
        text = str(JOBS[name])
        assert "setup-uv" not in text and "uv run" not in text and "python" not in text


def test_the_bot_branch_is_one_ci_accepts_and_ci_can_be_dispatched() -> None:
    script = CI["jobs"]["branch-name"]["steps"][0]["run"]
    types = re.search(r"types='([^']+)'", script)
    assert types
    assert re.fullmatch(rf"({types.group(1)})/[a-z0-9._-]{{1,60}}", FLOW["env"]["BRANCH"])
    assert "workflow_dispatch" in triggers(CI)
    assert CI["jobs"]["gate"]["name"] == "CI gate"
    assert "gh workflow run ci.yml" in JOBS["dispatch-ci"]["steps"][0]["run"]


def test_the_bot_commits_as_the_platform_bot() -> None:
    commit = next(s for _, s in steps() if s.get("name", "").startswith("Commit"))
    assert 'git config user.name "github-actions[bot]"' in commit["run"]
    assert "41898282+github-actions[bot]@users.noreply.github.com" in commit["run"]
