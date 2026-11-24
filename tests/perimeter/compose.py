# ruff: noqa: S603, S607  (every subprocess is the docker CLI with an argv built from constants)
"""A thin driver for the reference Compose stack under test (project `sg-perim`, no host ports).

Everything goes through the `docker` CLI so the tests exercise the same artefacts a user runs.
App-side actions (HTTP calls, DNS and TCP probes) run *inside* the `app` container with `exec`,
because the data port is deliberately not published on the host.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

PROJECT = "sg-perim"
REPO = Path(__file__).resolve().parents[2]
COMPOSE_DIR = REPO / "deploy" / "compose"
GUARD = "http://spendgate:8080"

# Runs inside the app container: argv = path, key ("" for none), body ("" for GET). Prints one JSON
# line with the HTTP status and the error code (None on success), never the response body.
_REQUEST_SCRIPT = f"""
GUARD = {GUARD!r}
import json, sys, urllib.error, urllib.request
path, key, body = sys.argv[1:4]
headers = {{"content-type": "application/json"}}
if key:
    headers["authorization"] = "Bearer " + key
req = urllib.request.Request(GUARD + path, data=body.encode() if body else None, headers=headers)
try:
    resp = urllib.request.urlopen(req, timeout=60)
    status, text = resp.status, resp.read().decode()
except urllib.error.HTTPError as exc:
    status, text = exc.code, exc.read().decode()
try:
    code = json.loads(text)["error"]["code"]
except (ValueError, KeyError, TypeError):
    code = None
print(json.dumps({{"status": status, "code": code}}))
"""


def docker_ready() -> bool:
    """True when the docker CLI, its daemon and the compose plugin all answer."""
    if shutil.which("docker") is None:
        return False
    for args in (["info"], ["compose", "version"]):
        done = subprocess.run(
            ["docker", *args],
            capture_output=True,
            check=False,
            timeout=60,
        )
        if done.returncode != 0:
            return False
    return True


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    code: str | None


@dataclass(slots=True)
class Compose:
    """One compose project. `files` are layered in order; `env` feeds ${VARS} in them."""

    files: list[Path]
    env: Mapping[str, str]
    profiles: Sequence[str] = ()
    started: bool = field(default=False, init=False)

    def _base(self) -> list[str]:
        cmd = ["docker", "compose", "-p", PROJECT, "--project-directory", str(COMPOSE_DIR)]
        for profile in self.profiles:
            cmd += ["--profile", profile]
        for file in self.files:
            cmd += ["-f", str(file)]
        return cmd

    def run(
        self, *args: str, timeout: int = 600, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        done = subprocess.run(
            [*self._base(), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env={**os.environ, **self.env},
        )
        if check and done.returncode != 0:
            raise RuntimeError(
                f"docker compose {' '.join(args)} failed ({done.returncode}):\n"
                f"{done.stdout}\n{done.stderr}"
            )
        return done

    # --- lifecycle -------------------------------------------------------------------------

    def up(self, *services: str, extra: Sequence[str] = ()) -> None:
        self.started = True  # even a half-started stack must be torn down
        self.run("build", "spendgate")  # the other services reuse the tag; build it first
        self.run("up", "-d", "--wait", *extra, *services)

    def start_stack(self, *, postgres: bool) -> None:
        """Migrate as the README does, then everything that should stay up, then the demo app,
        which runs once and exits."""
        self.started = True
        self.run("build", "spendgate")
        self.run("run", "--rm", "spendgate", "migrate")  # `serve` on Postgres does not migrate
        self.up("spendgate", "app", "fake-provider", *(("db",) if postgres else ()))
        self.run("up", "-d", "--no-deps", "demo")  # no --wait: it exits by design

    def exit_code(self, service: str) -> int:
        """Exit code of a one-shot service, waiting for it to finish."""
        done = subprocess.run(
            ["docker", "wait", f"{PROJECT}-{service}-1"],
            capture_output=True, text=True, check=True, timeout=60,
        )  # fmt: skip
        return int(done.stdout.strip())

    def down(self) -> None:
        # Always runs, even when `up` failed part-way; -v drops the ledger volume.
        self.run("down", "-v", "--remove-orphans", check=False, timeout=180)
        self.started = False

    # --- inside the stack ------------------------------------------------------------------

    def exec_python(self, service: str, code: str, *argv: str) -> str:
        return self.run("exec", "-T", service, "python", "-c", code, *argv).stdout

    def request(
        self, path: str, *, key: str = "", body: Mapping[str, object] | None = None
    ) -> Reply:
        """An HTTP call to the guard from the app container."""
        text = json.dumps(body) if body is not None else ""
        out = self.exec_python("app", _REQUEST_SCRIPT, path, key, text)
        data = json.loads(out)
        return Reply(int(data["status"]), data["code"])

    def upstream_calls(self) -> int:
        """How many requests have reached the fake provider since it started."""
        code = "import urllib.request as u; print(u.urlopen('http://127.0.0.1:9000/__log').read().decode())"
        return int(json.loads(self.exec_python("fake-provider", code))["count"])

    def cli(self, *args: str) -> str:
        """The spendgate CLI against the ledger directly (--local), as the README documents."""
        return self.run("exec", "-T", "spendgate", "spendgate", *args, "--local").stdout

    def create_key(self, scope: str, name: str, *extra: str) -> str:
        out = self.cli("keys", "create", "--scope", scope, "--name", name, "--json", *extra)
        secret = json.loads(out)["secret"]
        assert isinstance(secret, str)
        return secret

    # --- inspection ------------------------------------------------------------------------

    def inspect_all(self) -> list[dict[str, object]]:
        """`docker inspect` of every container of the project, one-off runs included."""
        ids = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"],
            capture_output=True, text=True, check=True,
        ).stdout.split()  # fmt: skip
        if not ids:
            return []
        out = subprocess.run(
            ["docker", "inspect", *ids],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        found = json.loads(out)
        assert isinstance(found, list)
        return found
