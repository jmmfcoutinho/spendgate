"""Shared fixtures: injected clock, deterministic ids, temp SQLite, Postgres URL, socket guard."""

import itertools
import os
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

POSTGRES_URL_ENV = "SPENDGATE_TEST_POSTGRES_URL"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


# 2026-01-01T00:00:00Z in epoch milliseconds, the unit used throughout spendgate.core.
CLOCK_START_MS = 1_767_225_600_000


class FakeClock:
    """Frozen time (integer epoch milliseconds) that only moves when a test says so."""

    def __init__(self, start: int = CLOCK_START_MS) -> None:
        self._now = start

    def now(self) -> int:
        return self._now

    def advance(self, ms: int = 0, *, seconds: int = 0) -> None:
        if ms < 0 or seconds < 0:
            raise ValueError("the clock never goes backwards unless a test sets it")
        self._now += ms + seconds * 1000

    def set(self, ms: int) -> None:
        self._now = ms


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def new_id() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"id-{next(counter):06d}"


@pytest.fixture
def sqlite_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"


@pytest.fixture
def postgres_url() -> str:
    url = os.environ.get(POSTGRES_URL_ENV)
    if not url:
        pytest.skip(f"{POSTGRES_URL_ENV} is not set")
    return url


def _host_of(address: object) -> str | None:
    """Host of an inet socket address; None for unix sockets (str/bytes paths)."""
    if isinstance(address, tuple) and address and isinstance(address[0], str):
        return address[0]
    return None


@pytest.fixture(autouse=True)
def _socket_guard(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Block network access: nothing outside `live`; loopback only for `postgres` and `docker`."""
    if request.node.get_closest_marker("live"):
        yield
        return
    loopback_ok = any(request.node.get_closest_marker(m) for m in ("postgres", "docker"))
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> None:  # noqa: ANN401 - mirrors stdlib signature
        host = _host_of(address)
        if host is not None and not (loopback_ok and host in _LOOPBACK):
            # Close it: callers that swallow the error (e.g. a download retry) never would.
            self.close()
            raise RuntimeError(f"network access blocked in tests: {host}")
        real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    # connect_ex and create_connection (via connect) must not be a way around the guard.
    monkeypatch.setattr(
        socket.socket, "connect_ex", lambda self, address: _connect_ex(self, address, guarded)
    )
    yield


def _connect_ex(sock: socket.socket, address: Any, guarded: Callable[..., None]) -> int:  # noqa: ANN401
    guarded(sock, address)
    return 0
