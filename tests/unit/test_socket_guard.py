import socket

import pytest


def test_non_loopback_connect_is_blocked_before_reaching_the_network() -> None:
    with socket.socket() as sock, pytest.raises(RuntimeError, match="blocked"):
        sock.connect(("192.0.2.1", 80))  # TEST-NET-1: never routable


def test_loopback_connect_is_blocked_for_plain_tests() -> None:
    with socket.socket() as sock, pytest.raises(RuntimeError, match="blocked"):
        sock.connect(("127.0.0.1", 9))


def test_connect_ex_cannot_bypass_the_guard() -> None:
    with socket.socket() as sock, pytest.raises(RuntimeError, match="blocked"):
        sock.connect_ex(("192.0.2.1", 80))


@pytest.mark.postgres
def test_postgres_marker_allows_loopback_but_not_other_hosts() -> None:
    with socket.socket() as sock, pytest.raises(RuntimeError, match="blocked"):
        sock.connect(("192.0.2.1", 80))
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        with socket.socket() as client:
            client.connect(srv.getsockname())


def test_unix_sockets_stay_allowed(tmp_path: object) -> None:
    path = f"{tmp_path}/s.sock"
    with socket.socket(socket.AF_UNIX) as srv:
        srv.bind(path)
        srv.listen(1)
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(path)
