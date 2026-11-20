"""Adapter fixtures for the ledger contract suite: the same tests run on every adapter."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.contract.rig import POSTGRES_HOOK_READY, Rig, workdir_location

# One entry per adapter. The Postgres entry runs only with
# `-m postgres` and SPENDGATE_TEST_POSTGRES_URL set, and needs the branches marked HOOK in
# tests/contract/rig.py (open_ledger, close_ledger, workdir_location) filled in.
ADAPTERS = [
    pytest.param("sqlite", id="sqlite"),
    pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
]


@pytest.fixture(params=ADAPTERS)
def adapter(request: pytest.FixtureRequest) -> str:
    kind: str = request.param
    if kind == "postgres":
        request.getfixturevalue("postgres_url")  # skips when the server is not configured
        if not POSTGRES_HOOK_READY:
            pytest.skip("Postgres contract hook not filled in yet")
    return kind


@pytest.fixture
def rig(adapter: str, tmp_path: Path, new_id: Callable[[], str]) -> Iterator[Rig]:
    made = Rig(adapter, workdir_location(adapter, tmp_path), new_id)
    yield made
    made.close()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """An unclosed SQLite connection fails the contract tests (no pyproject change needed).

    A leaked connection is reported by its finaliser, which pytest surfaces as an unraisable
    exception warning, so both warning classes are made errors.
    """
    here = Path(__file__).parent
    for item in items:
        if here in Path(str(item.path)).parents:
            item.add_marker(pytest.mark.filterwarnings("error::ResourceWarning"))
            item.add_marker(
                pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
            )
