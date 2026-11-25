# ruff: noqa: S603, S607  (every subprocess is the docker CLI with an argv built from constants)
"""The guard container's own hardening (C-26, TH-16), tested on the image and the running stack.

Test the claim, not the config: the image is run without Compose to see its default user, and
the running guard is inspected and probed from inside.
"""

import json
import subprocess

import pytest

from tests.perimeter.conftest import Stack

pytestmark = pytest.mark.docker

IMAGE = "spendgate:core"  # the tag the SQLite stack builds (SPENDGATE_VARIANT unset)

# Runs inside the guard. Reads the guard process itself (PID 1, the same user, so readable): its
# identity, capability sets and the root mount's options; then what this user can write.
_PROBE = """
import json, os
status = dict(
    line.split(":", 1) for line in open("/proc/1/status").read().splitlines() if ":" in line
)
keys = ("Uid", "Gid", "CapEff", "CapPrm", "CapBnd", "NoNewPrivs")
out = {key: status[key].split() for key in keys}
mounts = [line.split() for line in open("/proc/1/mounts").read().splitlines()]
out["root"] = [m[3].split(",") for m in mounts if m[1] == "/"]
out["writes"] = {}
for path in ("/app/probe", "/etc/probe", "/probe", "/data/probe"):
    try:
        with open(path, "w") as fh:
            fh.write("x")
        os.remove(path)
        out["writes"][path] = "ok"
    except OSError as exc:
        out["writes"][path] = type(exc).__name__
print(json.dumps(out))
"""


def _guard(stack: Stack) -> dict[str, object]:
    """`docker inspect` of the long-running guard container (not a one-off `run`)."""
    found = [
        c for c in stack.compose.inspect_all()
        if c["Config"]["Labels"]["com.docker.compose.service"] == "spendgate"  # type: ignore[index]
        and c["Config"]["Labels"].get("com.docker.compose.oneoff") != "True"  # type: ignore[index]
    ]  # fmt: skip
    assert len(found) == 1, [c["Name"] for c in found]
    return found[0]


def test_t_ctr_01_the_image_runs_as_uid_10001_without_being_told(sqlite_stack: Stack) -> None:
    configured = subprocess.run(
        ["docker", "image", "inspect", IMAGE, "--format", "{{.Config.User}}"],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout.strip()  # fmt: skip
    assert configured == "10001:10001"
    # No --user: what the image does on its own, with no network and nothing mounted.
    ran = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", IMAGE,
         "-c", "import os; print(os.getuid(), os.getgid(), os.geteuid())"],
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout.split()  # fmt: skip
    assert ran == ["10001", "10001", "10001"]
    guard = json.loads(sqlite_stack.compose.exec_python("spendgate", _PROBE))
    assert guard["Uid"] == guard["Gid"] == ["10001"] * 4  # real, effective, saved, filesystem


def test_t_ctr_02_the_guard_runs_read_only_with_no_capabilities(sqlite_stack: Stack) -> None:
    host = _guard(sqlite_stack)["HostConfig"]
    assert isinstance(host, dict)
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"] and not host.get("CapAdd")
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert host["Privileged"] is False
    assert host["Memory"] == 512 * 1024 * 1024 and host["PidsLimit"] == 128
    guard = json.loads(sqlite_stack.compose.exec_python("spendgate", _PROBE))
    no_caps = ["0000000000000000"]
    assert guard["CapEff"] == guard["CapPrm"] == guard["CapBnd"] == no_caps
    assert guard["NoNewPrivs"] == ["1"]
    assert guard["root"] and all("ro" in options for options in guard["root"])
    writes = guard["writes"]
    assert writes.pop("/data/probe") == "ok"  # the ledger volume is the one writable place
    assert set(writes.values()) <= {"OSError", "PermissionError"}, writes  # EROFS or EACCES
