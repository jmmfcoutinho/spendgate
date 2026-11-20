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
