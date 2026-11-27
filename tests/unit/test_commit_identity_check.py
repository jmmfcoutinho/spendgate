"""scripts/check-commit-identity.sh: a commit must be authored and committed by a person."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-commit-identity.sh"


BASH = shutil.which("bash") or "/bin/bash"


def check(idents: str) -> int:
    run = subprocess.run(  # noqa: S603 - fixed script, test-controlled input on stdin
        [BASH, str(SCRIPT), "-"], input=idents, capture_output=True, text=True, check=False
    )
    return run.returncode


@pytest.mark.parametrize(
    "ident",
    [
        "Ada Lovelace <ada@example.org>",
        "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>",
        # The daily price watch's commit (.github/workflows/price-watch.yml).
        "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>",
        "GitHub <noreply@github.com>",
        "Claudette Marsh <cm@example.org>",  # a name that merely starts like an assistant's
    ],
)
def test_a_person_or_the_platform_may_author_and_commit(ident: str) -> None:
    assert check(f"{ident}\n{ident}\n") == 0


@pytest.mark.parametrize(
    "ident",
    [
        "Claude <noreply@anthropic.com>",
        "Someone <someone@anthropic.com>",
        "Copilot <198982749+Copilot@users.noreply.github.com>",
        "codex <codex@example.org>",
        "Ada Lovelace (via ChatGPT) <ada@example.org>",
        "Bot <bot@openai.com>",
    ],
)
def test_an_assistant_identity_is_rejected_as_author_or_committer(ident: str) -> None:
    person = "Ada Lovelace <ada@example.org>"
    assert check(f"{ident}\n{person}\n") == 1
    assert check(f"{person}\n{ident}\n") == 1
