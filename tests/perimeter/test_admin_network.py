"""The admin API on its own network (deploy/compose/compose.admin.yaml), in the running stack.

With `server.admin.listen: spendgate-admin:8081` the admin listener binds the guard's address on the
internal `admin` network only. The claim: the operator service on `admin` opens a connection to it,
and an app on `apps` cannot, by the guard's name, by the admin name, or by either address.
"""

import json

import pytest

from tests.perimeter.compose import ADMIN_HOST, ADMIN_PORT
from tests.perimeter.conftest import Stack

pytestmark = pytest.mark.docker

# Runs in a container. argv: JSON list of [host, port]. Prints {"host:port": "connected" or "fails"}
_TCP = """
import json, socket, sys
out = {}
for host, port in json.loads(sys.argv[1]):
    try:
        socket.create_connection((host, port), timeout=3).close()
        out[f"{host}:{port}"] = "connected"
    except OSError:  # refused, unreachable, timed out, or a name that does not resolve
        out[f"{host}:{port}"] = "fails"
print(json.dumps(out))
"""

# Runs in the operator container: the admin app answers there (`/metrics` is open on its own port).
_METRICS = """
import sys, urllib.request
print(urllib.request.urlopen(sys.argv[1] + "/metrics", timeout=10).status)
"""


def _probe(stack: Stack, service: str, targets: list[tuple[str, int]]) -> dict[str, str]:
    argv = json.dumps(targets)
    if service == "operator":  # profile `operator`: not kept running, so a one-off container
        out = stack.compose.run_python(service, _TCP, argv)
    else:
        out = stack.compose.exec_python(service, _TCP, argv)
    found = json.loads(out.strip().splitlines()[-1])  # the probe prints one line, last
    assert isinstance(found, dict)
    return found


def _guard_addresses(stack: Stack) -> tuple[str, str]:
    """The guard's address on `apps` and on `admin`, as an app that learned them would use."""
    nets = stack.compose.addresses("spendgate")
    assert nets["apps"] and nets["admin"], nets
    return nets["apps"], nets["admin"]


# T-ADM-03
def test_an_app_cannot_reach_the_admin_port_on_the_admin_network(
    admin_stack: Stack,
) -> None:
    apps_ip, admin_ip = _guard_addresses(admin_stack)
    probe = _probe(
        admin_stack,
        "app",
        [
            ("spendgate", 8080),  # the data port: the probe can connect at all
            ("spendgate", ADMIN_PORT),
            (ADMIN_HOST, ADMIN_PORT),  # the alias exists on `admin` only, so it does not resolve
            (apps_ip, ADMIN_PORT),
            (admin_ip, ADMIN_PORT),  # no route from `apps` to `admin`
        ],
    )
    assert probe == {
        "spendgate:8080": "connected",
        f"spendgate:{ADMIN_PORT}": "fails",
        f"{ADMIN_HOST}:{ADMIN_PORT}": "fails",
        f"{apps_ip}:{ADMIN_PORT}": "fails",
        f"{admin_ip}:{ADMIN_PORT}": "fails",
    }


# T-ADM-03
def test_the_operator_on_the_admin_network_reaches_the_admin_port(admin_stack: Stack) -> None:
    _apps_ip, admin_ip = _guard_addresses(admin_stack)
    probe = _probe(
        admin_stack,
        "operator",
        [(ADMIN_HOST, ADMIN_PORT), (admin_ip, ADMIN_PORT), ("1.1.1.1", 443)],
    )
    assert probe == {
        f"{ADMIN_HOST}:{ADMIN_PORT}": "connected",
        f"{admin_ip}:{ADMIN_PORT}": "connected",
        "1.1.1.1:443": "fails",  # `admin` is internal too: the operator has no internet either
    }
    url = f"http://{ADMIN_HOST}:{ADMIN_PORT}"
    assert admin_stack.compose.run_python("operator", _METRICS, url).split()[-1] == "200"


def test_the_admin_listener_binds_the_admin_address_only(admin_stack: Stack) -> None:
    """Inside the guard's container the configured host is honoured: not loopback, not 0.0.0.0."""
    _apps_ip, admin_ip = _guard_addresses(admin_stack)
    probe = _probe(
        admin_stack,
        "spendgate",
        [("127.0.0.1", 8080), ("127.0.0.1", ADMIN_PORT), (admin_ip, ADMIN_PORT)],
    )
    assert probe == {
        "127.0.0.1:8080": "connected",
        f"127.0.0.1:{ADMIN_PORT}": "fails",
        f"{admin_ip}:{ADMIN_PORT}": "connected",
    }
