"""The perimeter claims of deploy/compose, tested against the running stack.

Test the claim, not the config: containers are inspected and probed after `up`, whatever the compose
file says. The negative tests leak a key on purpose and require the checker to notice.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from tests.perimeter.compose import COMPOSE_DIR, GUARD
from tests.perimeter.conftest import Stack
from tests.perimeter.leaks import credential_names, find_leaks

pytestmark = pytest.mark.docker

# Runs inside the app container. Prints one JSON object; every probe must fail except the guard's.
_PROBE = """
import json, socket, sys, urllib.request
hosts, guard = json.loads(sys.argv[1]), sys.argv[2]
out = {"dns": {}, "tcp": {}, "guard": {}, "files": {}}
for host in hosts:
    try:
        socket.getaddrinfo(host, 443)
        out["dns"][host] = "resolved"
    except socket.gaierror:
        out["dns"][host] = "fails"
for ip, port in (("1.1.1.1", 443), ("8.8.8.8", 53), ("93.184.216.34", 80)):
    try:
        socket.create_connection((ip, port), timeout=3).close()
        out["tcp"][ip] = "connected"
    except OSError:
        out["tcp"][ip] = "fails"
for path in ("/livez", "/readyz"):
    try:
        out["guard"][path] = urllib.request.urlopen(guard + path, timeout=5).status
    except OSError as exc:
        out["guard"][path] = str(exc)
try:
    socket.create_connection(("spendgate", 8081), timeout=3).close()
    out["guard"]["admin"] = "connected"
except OSError:
    out["guard"]["admin"] = "fails"
import os
out["files"]["run_secrets"] = os.path.exists("/run/secrets")
out["files"]["config"] = os.path.exists("/etc/spendgate")
print(json.dumps(out))
"""


def _provider_hosts() -> list[str]:
    """Every provider hostname the guard could be configured with, plus the two best known."""
    hosts = {"api.openai.com", "api.anthropic.com", "api.elevenlabs.io"}
    for config in COMPOSE_DIR.glob("spendgate.*.yaml"):
        for provider in yaml.safe_load(config.read_text())["providers"].values():
            hosts.add(provider["base_url"].split("//", 1)[1].split("/", 1)[0].split(":")[0])
    return sorted(hosts)


def _leaks(stack: Stack) -> list[str]:
    return find_leaks(
        stack.compose.inspect_all(), credential_names(COMPOSE_DIR), stack.secret_values
    )


# T-NET-01
def test_no_credential_outside_the_guard(sqlite_stack: Stack) -> None:
    assert _leaks(sqlite_stack) == []
    # The scan has teeth: the guard itself does hold the secrets, so names and mounts are real.
    guard = next(
        b for b in sqlite_stack.compose.inspect_all()
        if b["Config"]["Labels"]["com.docker.compose.service"] == "spendgate"  # type: ignore[index]
    )  # fmt: skip
    mounted = {m["Destination"] for m in guard["Mounts"]}  # type: ignore[attr-defined]
    assert {"/run/secrets/openai_api_key", "/run/secrets/spendgate_key_pepper"} <= mounted


# T-NET-02, T-NET-03
def test_app_cannot_reach_the_internet_but_reaches_the_guard(sqlite_stack: Stack) -> None:
    probe = json.loads(
        sqlite_stack.compose.exec_python("app", _PROBE, json.dumps(_provider_hosts()), GUARD)
    )
    assert set(probe["dns"].values()) == {"fails"}, probe["dns"]
    assert set(probe["tcp"].values()) == {"fails"}, probe["tcp"]
    assert probe["guard"] == {"/livez": 200, "/readyz": 200, "admin": "fails"}
    assert probe["files"] == {"run_secrets": False, "config": False}


# Runs in either container: argv = base URL, then paths. Prints {path: status or error text}.
_GET = """
import json, sys, urllib.error, urllib.request
base, out = sys.argv[1], {}
for path in sys.argv[2:]:
    try:
        out[path] = urllib.request.urlopen(base + path, timeout=5).status
    except urllib.error.HTTPError as exc:
        out[path] = exc.code
    except OSError as exc:
        out[path] = type(exc).__name__
print(json.dumps(out))
"""


def test_t_adm_03_the_admin_port_listens_in_the_guard_but_apps_cannot_reach_it(
    sqlite_stack: Stack,
) -> None:
    compose = sqlite_stack.compose
    inside = json.loads(
        compose.exec_python(
            "spendgate", _GET, "http://127.0.0.1:8081", "/metrics", "/admin/v1/keys"
        )
    )
    assert inside == {"/metrics": 200, "/admin/v1/keys": 401}  # it is up, and it wants a key
    from_app = json.loads(compose.exec_python("app", _GET, "http://spendgate:8081", "/metrics"))
    assert isinstance(from_app["/metrics"], str), from_app  # no HTTP answer at all, only an error
    data_port = json.loads(
        compose.exec_python("app", _GET, GUARD, "/metrics", "/admin/v1/keys", "/livez")
    )
    assert data_port == {"/metrics": 404, "/admin/v1/keys": 404, "/livez": 200}


@pytest.fixture
def restore_app(sqlite_stack: Stack) -> Iterator[None]:
    yield
    sqlite_stack.compose.up("app", extra=("--no-deps", "--force-recreate"))


_FAKE_KEY = "sk-" + "x" * 40  # shaped like a key; not one


def _leak_overlay(stack: Stack, name: str, content: dict[str, object]) -> Path:
    path = stack.workdir / f"leak-{name}.yaml"
    path.write_text(yaml.safe_dump({"services": {"app": content}}))
    return path


LEAKS: dict[str, tuple[dict[str, object], str]] = {
    # A provider the old guard's OPENAI*/AZURE* check missed.
    "elevenlabs-env": ({"environment": {"ELEVENLABS_API_KEY": _FAKE_KEY}}, "ELEVENLABS_API_KEY"),
    # An innocuous name: only the value shape gives it away.
    "key-shaped-value": ({"environment": {"NOTES": _FAKE_KEY}}, "NOTES"),
    # The guard's own secret file mounted into an app.
    "secret-mount": ({"secrets": ["openai_api_key"]}, "/run/secrets/openai_api_key"),
}


# T-NET-04: a deliberately leaked provider key makes the no-keys-in-apps check (T-NET-01) fail
@pytest.mark.usefixtures("restore_app")
@pytest.mark.parametrize("name", sorted(LEAKS))
def test_a_leaked_key_in_an_app_container_fails_the_check(sqlite_stack: Stack, name: str) -> None:
    content, expected = LEAKS[name]
    overlay = _leak_overlay(sqlite_stack, name, content)
    leaky = type(sqlite_stack.compose)(
        files=[*sqlite_stack.compose.files, overlay],
        env=sqlite_stack.compose.env,
        profiles=sqlite_stack.compose.profiles,
    )
    leaky.up("app", extra=("--no-deps", "--force-recreate"))
    findings = _leaks(sqlite_stack)
    assert findings, f"the checker missed the leak {name!r}"
    assert any(expected in f and f.startswith("app ") for f in findings), findings


def test_demo_app_reports_the_perimeter_holds(sqlite_stack: Stack) -> None:
    """The shipped demo container agrees with the probes above."""
    assert sqlite_stack.compose.exit_code("demo") == 0
    logs = sqlite_stack.compose.run("logs", "demo").stdout
    assert "api.openai.com does not resolve" in logs
