from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.conftest import FakeClock


def test_clock_is_frozen_until_advanced(clock: FakeClock) -> None:
    start = clock.now()
    assert clock.now() == start
    clock.advance(30)
    assert clock.now() == start + 30
    clock.advance(seconds=2)
    assert clock.now() == start + 2_030
    clock.advance(5, seconds=1)
    assert clock.now() == start + 3_035


def test_clock_starts_at_2026_01_01_in_integer_ms(clock: FakeClock) -> None:
    start = clock.now()
    assert isinstance(start, int)
    assert start == int(datetime(2026, 1, 1, tzinfo=UTC).timestamp()) * 1000


def test_clock_set_and_refuses_to_go_backwards(clock: FakeClock) -> None:
    clock.set(42)
    assert clock.now() == 42
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(seconds=-1)


def test_ids_are_deterministic_and_unique(new_id: Callable[[], str]) -> None:
    assert [new_id(), new_id()] == ["id-000001", "id-000002"]


def test_sqlite_path_is_fresh_and_not_created(sqlite_path: Path) -> None:
    assert not sqlite_path.exists()
