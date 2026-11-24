"""Every relative link in the published docs resolves."""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PAGES = [
    REPO / "README.md",
    REPO / "ROADMAP.md",
    *(
        REPO / "docs" / name
        for name in (
            "README.md",
            "how-money-is-protected.md",
            "configuration.md",
            "catalog-how-to.md",
            "provider-how-to.md",
            "operations.md",
            "litellm-comparison.md",
        )
    ),
]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)


def slug(heading: str) -> str:
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", text))


def anchors(path: Path) -> set[str]:
    text = FENCE.sub("", path.read_text())
    return {slug(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.*)$", text, re.MULTILINE)}


def links(page: Path) -> list[str]:
    return LINK.findall(FENCE.sub("", page.read_text()))


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(REPO)))
def test_relative_links_resolve(page: Path) -> None:
    broken = []
    for link in links(page):
        if re.match(r"[a-z]+:", link):
            continue  # http(s), mailto
        target, _, fragment = link.partition("#")
        resolved = (page.parent / target).resolve() if target else page
        missing = not resolved.exists()
        bad_anchor = (
            bool(fragment) and resolved.suffix == ".md" and fragment not in anchors(resolved)
        )
        if missing or bad_anchor:
            broken.append(link)
    assert not broken, f"{page.name}: broken links {broken}"


# PyPI renders README.md as the project page with no repository to resolve relative paths against,
# so the README links into the repository by absolute URL; these are then checked like local links.
REPO_URL = "https://github.com/jmmfcoutinho/spendgate"
REPO_FILE = re.compile(re.escape(REPO_URL) + r"/blob/main/([^#?]+)(?:#(.*))?$")
# Inline links and images (badges included), with or without a title or <angle brackets>.
MARKDOWN_TARGET = re.compile(r"\]\(\s*<?([^)\s>]+)")
REFERENCE_TARGET = re.compile(r"^\s*\[[^\]]+\]:\s*<?([^\s>]+)", re.MULTILINE)  # [id]: target
HTML_TARGET = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""")


def readme_targets() -> list[str]:
    text = FENCE.sub("", (REPO / "README.md").read_text())
    return [
        *MARKDOWN_TARGET.findall(text),
        *REFERENCE_TARGET.findall(text),
        *HTML_TARGET.findall(text),
    ]


def test_readme_has_no_relative_links_so_it_renders_on_pypi() -> None:
    relative = [t for t in readme_targets() if not t.startswith("https://")]
    assert not relative, f"README.md: use absolute https links, not {relative}"


def test_readme_links_into_the_repository_resolve() -> None:
    broken = []
    for target in readme_targets():
        found = REPO_FILE.match(target)
        if not found:
            continue
        path, fragment = REPO / found.group(1), found.group(2)
        bad_anchor = bool(fragment) and path.suffix == ".md" and fragment not in anchors(path)
        if not path.is_file() or bad_anchor:
            broken.append(target)
    assert not broken, f"README.md: links to missing repository files {broken}"
    assert any(REPO_FILE.match(t) for t in readme_targets()), "README.md has no repository links"
