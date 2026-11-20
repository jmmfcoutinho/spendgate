"""Supply-chain pins (threat-model C-14): T-SUP-01 no floating action tags or base images,
T-SUP-02 the lockfile is checked against `pyproject.toml` in CI."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)", re.M)
FROM = re.compile(r"^FROM\s+(?:--\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.M | re.I)
IMAGE = re.compile(r"^[ \t]*image:[ \t]+(\S+)", re.M)  # not the `image:` job key
SHA_REF = re.compile(r"@[0-9a-f]{40}$")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}")


def unpinned_actions(text: str) -> list[str]:
    """`uses:` references that are not a full commit SHA (a tag like `@v4` can be moved)."""
    return [u for u in USES.findall(text) if not u.startswith("./") and not SHA_REF.search(u)]


def unpinned_froms(text: str) -> list[str]:
    """`FROM` lines that name neither a digest, `scratch`, nor an earlier build stage."""
    stages: set[str] = set()
    bad: list[str] = []
    for image, alias in FROM.findall(text):
        if image != "scratch" and image not in stages and not DIGEST.search(image):
            bad.append(image)
        if alias:
            stages.add(alias)
    return bad


def unpinned_service_images(text: str) -> list[str]:
    """Workflow `image:` values (service containers) without a digest."""
    return [i for i in IMAGE.findall(text) if "${{" not in i and not DIGEST.search(i)]


def test_t_sup_01_the_pin_lint_catches_floating_references() -> None:
    assert unpinned_actions("      - uses: actions/checkout@v4\n") == ["actions/checkout@v4"]
    assert unpinned_actions("      - uses: actions/checkout@main # v4\n")
    sha = "3d3c42e5aac5ba805825da76410c181273ba90b1"  # pragma: allowlist secret (a commit SHA)
    assert unpinned_actions(f"      - uses: actions/checkout@{sha} # v7\n") == []
    assert unpinned_actions("      - uses: ./local-action\n") == []
    digest = "sha256:" + "a" * 64
    assert unpinned_froms("FROM python:3.13-slim AS build\n") == ["python:3.13-slim"]
    pinned = f"FROM python:3.13@{digest} AS build\nFROM build AS x\nFROM scratch\n"
    assert unpinned_froms(pinned) == []
    assert unpinned_froms(f"FROM --platform=linux/amd64 python:3.13@{digest}\n") == []
    assert unpinned_service_images("    image: postgres:17-alpine\n") == ["postgres:17-alpine"]


@pytest.mark.parametrize("workflow", sorted((ROOT / ".github" / "workflows").glob("*.y*ml")))
def test_t_sup_01_every_workflow_action_and_service_image_is_pinned(workflow: Path) -> None:
    text = workflow.read_text("utf-8")
    assert unpinned_actions(text) == [], f"{workflow.name}: pin actions by commit SHA"
    assert unpinned_service_images(text) == [], f"{workflow.name}: pin images by digest"


@pytest.mark.parametrize("dockerfile", sorted(ROOT.glob("Dockerfile*")))
def test_t_sup_01_every_dockerfile_base_image_is_pinned_by_digest(dockerfile: Path) -> None:
    assert unpinned_froms(dockerfile.read_text("utf-8")) == []


def test_t_sup_02_ci_checks_that_the_lockfile_matches_pyproject() -> None:
    """`uv sync --frozen` trusts the lock; one job must compare it with `pyproject.toml`."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    assert re.search(r"^\s*- run: uv lock --check\b", ci, re.M)
