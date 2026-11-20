# ruff: noqa: S603, S607  # the tests run the guide's own commands: that is the point
"""`docs/deploy/local.md` runs: the marked code blocks of each step are executed and checked.

Blocks are marked in the Markdown by `<!-- doctest: NAME -->` on the line before the fence.
Step 1 runs in-process: `spendgate.local.guard` is wrapped so the guide's own code gets the fake
provider as its HTTP client (no socket). Step 2 runs the guide's shell and Python blocks as real
processes (`spendgate serve`, the CLI, the OpenAI and Anthropic SDKs) against the fake provider
served on a loopback port; only three things are substituted, all listed below. Step 3 needs Docker.

Substitutions (everything else is the guide's text, verbatim):
- the placeholder provider keys in the guide are accepted as they are (the fake checks no key);
- the provider base URLs in `examples/config.local.yaml` and the gateway port 8080 are rewritten to
  free loopback ports;
- `uv sync` is a no-op and `uv run X` runs X from the test's own virtualenv (no network).
"""

import contextlib
import io
import os
import re
import socket
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import spendgate.local
from tests.fakes import FakeUpstream

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "deploy" / "local.md"
_BLOCK = re.compile(r"<!-- doctest: ([\w-]+) -->\n```(\w+)\n(.*?)```", re.DOTALL)
_SECRET = re.compile(r"sg-[A-Za-z0-9_-]+")
FAKE_ENV = {
    "OPENAI_API_KEY": "sk-doctest",  # pragma: allowlist secret
    "ANTHROPIC_API_KEY": "sk-ant-doctest",  # pragma: allowlist secret
}


def blocks() -> dict[str, str]:
    found = {name: code for name, _lang, code in _BLOCK.findall(GUIDE.read_text())}
    assert len(found) == len(_BLOCK.findall(GUIDE.read_text())), "duplicate doctest marker"
    return found


def test_the_guide_marks_every_block_the_tests_expect() -> None:
    assert set(blocks()) == {
        "step1", "step1-expect", "step2-env", "step2-setup", "step2-serve", "step2-key",
        "step2-python", "step2-run", "step2-usage",
        "step3-up", "step3-verify", "step3-key", "step3-use",
    }  # fmt: skip


def test_relative_links_in_the_guide_resolve() -> None:
    links = re.findall(r"\]\((?!https?:|#)([^)#]+)", GUIDE.read_text())
    assert links
    for link in links:
        assert (GUIDE.parent / link).exists(), f"broken link in the guide: {link}"


def test_the_guide_names_the_files_the_tests_write() -> None:
    text = GUIDE.read_text()
    assert "Save as `guard_demo.py`" in text and "`proxy_demo.py`" in text


# --- step 1: library mode, in process ------------------------------------------------------


def test_step1_a_call_over_the_cap_is_refused_with_budget_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeUpstream()
    real_guard = spendgate.local.guard

    def guard_on_the_fake(*args: object, **kw: object) -> spendgate.local.Guard:
        return real_guard(*args, client=fake.client(), **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(spendgate.local, "guard", guard_on_the_fake)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_ENV["OPENAI_API_KEY"])
    monkeypatch.chdir(tmp_path)  # the guide's ledger is a relative file
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(blocks()["step1"], "local.md#step1", "exec"), {"__name__": "__main__"})  # noqa: S102
    assert out.getvalue() == blocks()["step1-expect"]
    assert fake.calls == 1  # the refused call never reached the provider
    assert (tmp_path / "guide-ledger.db").exists()


# --- step 2: the local proxy, as real processes --------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for_port(port: int, timeout: float = 30.0) -> None:
    """Poll from a child process: the test process may not open sockets (conftest socket guard)."""
    code = (
        "import socket,sys,time\n"
        f"end=time.time()+{timeout}\n"
        "while time.time()<end:\n"
        "    try:\n"
        f"        socket.create_connection(('127.0.0.1',{port}),0.5).close(); sys.exit(0)\n"
        "    except OSError:\n"
        "        time.sleep(0.2)\n"
        "sys.exit(1)\n"
    )
    assert subprocess.run([sys.executable, "-I", "-c", code], check=False).returncode == 0


class Workdir:
    """A scratch copy of what the guide's commands expect: `examples/config.local.yaml`, `uv`."""

    def __init__(self, path: Path, gateway: int, fake: int) -> None:
        self.path, self.gateway = path, gateway
        config = (ROOT / "examples" / "config.local.yaml").read_text()
        config = config.replace("127.0.0.1:8080", f"127.0.0.1:{gateway}")
        config = config.replace("https://api.openai.com/v1", f"http://127.0.0.1:{fake}/v1")
        config = config.replace("https://api.anthropic.com", f"http://127.0.0.1:{fake}")
        assert f":{fake}/v1" in config and f"127.0.0.1:{gateway}" in config
        (path / "examples").mkdir()
        (path / "examples" / "config.local.yaml").write_text(config)
        shim = path / "bin"
        shim.mkdir()
        uv = shim / "uv"
        uv.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  sync) exit 0 ;;\n"
            f'  run) shift; cmd="$1"; shift; exec "{Path(sys.executable).parent}/$cmd" "$@" ;;\n'
            "esac\nexit 2\n"
        )
        uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
        self.env = {
            **os.environ, **FAKE_ENV, "PATH": f"{shim}:{os.environ['PATH']}", "HOME": str(path),
            "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1",
        }  # fmt: skip

    def text(self, code: str) -> str:
        return code.replace("127.0.0.1:8080", f"127.0.0.1:{self.gateway}")

    def run(self, code: str, preamble: str = "") -> subprocess.CompletedProcess[str]:
        script = self.text(preamble) + self.text(code)
        return subprocess.run(
            ["bash", "-ec", script], cwd=self.path, env=self.env, capture_output=True, text=True,
            timeout=120, check=False,
        )  # fmt: skip

    def spawn(self, code: str, preamble: str, log: Path) -> subprocess.Popen[bytes]:
        with log.open("wb") as sink:
            return subprocess.Popen(
                ["bash", "-ec", self.text(preamble) + "exec " + self.text(code)],
                cwd=self.path, env=self.env, stdout=sink, stderr=subprocess.STDOUT,
            )  # fmt: skip


@contextlib.contextmanager
def fake_provider(port: int) -> Iterator[None]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.docs.fake_provider", str(port)], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        wait_for_port(port)
        yield
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_step2_the_proxy_serves_both_sdks_and_usage_shows_the_calls(tmp_path: Path) -> None:
    guide = blocks()
    fake_port, gateway = free_port(), free_port()
    work = Workdir(tmp_path, gateway, fake_port)
    env = guide["step2-env"]
    with fake_provider(fake_port):
        for name in ("step2-env", "step2-setup"):
            done = work.run(guide[name], env)
            assert done.returncode == 0, done.stderr
        assert "configuration ok" in done.stdout + done.stderr
        server = work.spawn(guide["step2-serve"], env, tmp_path / "serve.log")
        try:
            wait_for_port(gateway)
            made = work.run(guide["step2-key"], env)
            assert made.returncode == 0, made.stderr
            secret = _SECRET.search(made.stdout)
            assert secret, made.stdout
            work.env["SPENDGATE_API_KEY"] = secret.group(0)
            (tmp_path / "proxy_demo.py").write_text(work.text(guide["step2-python"]))
            ran = work.run(guide["step2-run"], env)
            assert ran.returncode == 0, ran.stderr
            assert "openai ok:" in ran.stdout and "anthropic ok:" in ran.stdout
            usage = work.run(guide["step2-usage"], env)
            assert usage.returncode == 0, usage.stderr
            assert "gpt-6-luna" in usage.stdout and "claude-haiku" in usage.stdout
        finally:
            server.terminate()
            server.wait(timeout=15)


# --- step 3: Docker Compose ----------------------------------------------------------------

COMPOSE = ROOT / "deploy" / "compose"


@pytest.mark.docker
def test_step3_the_perimeter_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    if subprocess.run(["docker", "info"], capture_output=True, check=False).returncode != 0:
        pytest.skip("Docker is not available")
    secrets = COMPOSE / "secrets"
    if any(p.name != ".gitignore" for p in secrets.glob("*")):
        pytest.skip("deploy/compose/secrets already holds files; not overwriting them")
    guide = blocks()
    env = {**os.environ, **FAKE_ENV, "COMPOSE_PROJECT_NAME": "sg-guide"}  # not the file's name

    def sh(code: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-ec", code], cwd=cwd, env=env, capture_output=True, text=True,
            timeout=900, check=False,
        )  # fmt: skip

    try:
        up = sh(guide["step3-up"], ROOT)
        assert up.returncode == 0, up.stderr
        verify = sh(guide["step3-verify"], COMPOSE)
        assert verify.returncode == 0, verify.stdout + verify.stderr
        assert "-> 200" in verify.stdout
        assert verify.stdout.count("the perimeter holds") == 2
        made = sh(guide["step3-key"], COMPOSE)
        assert made.returncode == 0, made.stderr
        secret = _SECRET.search(made.stdout)
        assert secret, made.stdout
        used = sh(guide["step3-use"].replace("sg-...", secret.group(0)), COMPOSE)
        assert used.returncode == 0, used.stdout + used.stderr
        assert "with SPENDGATE_API_KEY -> 200" in used.stdout
    finally:
        sh("docker compose down -v", COMPOSE)
        for p in secrets.glob("*"):
            if p.name != ".gitignore":
                p.unlink()
