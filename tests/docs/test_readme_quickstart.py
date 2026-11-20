"""The README quickstart runs as written, against the fake provider, with no real key or network.

A block is executable when the line before its fence is an HTML comment `<!-- doctest: KIND -->`:

- `file NAME`: written to NAME in a scratch directory.
- `shell`: run with bash in that directory; exported variables carry over to later blocks.
- `serve`: started in the background (`spendgate serve`).
- `shell` blocks that run `migrate --bootstrap` print the data key; the test reads it from stderr.
- `python`: run in a fresh interpreter against the running proxy.
- `library`: run in this process with the local guard pointed at the fake provider.

Only two things are substituted: the provider address (the fake's) and the loopback port, and the
`sg-...` placeholder becomes the key the proxy printed. Everything else is the README's text, so
a renamed command, flag, config key or import breaks this test.

The proxy and the fake provider run as subprocesses on 127.0.0.1: the suite's socket guard only
covers this process, and the guard exists so tests cannot reach the internet, which these never
do. The in-process library block opens no socket at all.
"""

import contextlib
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

import spendgate.local
from tests.fakes import FakeUpstream
from tests.integration.harness import NOW_MS

REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"
MARKER = re.compile(r"^<!-- doctest: (\w+)(?: (\S+))? -->$")
KEY_LINE = re.compile(r"created api key quickstart \(org:demo\): (sg-\S+)")
REAL_UPSTREAM = "https://api.openai.com/v1"
README_PORT = "127.0.0.1:8080"
PLACEHOLDER_KEY = "sg-..."

FAKE_SERVER = (
    "import sys, uvicorn\n"
    "from tests.fakes import FakeUpstream\n"
    "uvicorn.run(FakeUpstream().app, host='127.0.0.1', port=int(sys.argv[1]),"
    " log_level='warning')\n"
)
# Any HTTP answer, even a 404, means the server is up. Runs in a child process, so its short real
# sleeps never touch this process's clock.
WAIT_READY = (
    "import sys, time, urllib.error, urllib.request\n"
    "end = time.monotonic() + 40\n"
    "while time.monotonic() < end:\n"
    "    try:\n"
    "        urllib.request.urlopen(sys.argv[1], timeout=2).close()\n"
    "        sys.exit(0)\n"
    "    except urllib.error.HTTPError:\n"
    "        sys.exit(0)\n"
    "    except OSError:\n"
    "        time.sleep(0.1)\n"
    "sys.exit(1)\n"
)
SHIM = (
    "#!/bin/sh\n"
    "exec {python} -c 'import sys; from spendgate.cli import main; sys.exit(main())' \"$@\"\n"
)
BASH = shutil.which("bash") or "/bin/bash"


@dataclass(frozen=True)
class Block:
    kind: str
    arg: str | None
    text: str


def parse_blocks(text: str) -> list[Block]:
    lines, blocks, i = text.splitlines(), [], 0
    while i < len(lines):
        found = MARKER.match(lines[i])
        i += 1
        if not found:
            continue
        while lines[i].strip() == "":
            i += 1
        assert lines[i].startswith("```"), f"{found.group(0)} must be followed by a fenced block"
        i += 1
        body = []
        while not lines[i].startswith("```"):
            body.append(lines[i])
            i += 1
        blocks.append(Block(found.group(1), found.group(2), "\n".join(body) + "\n"))
    return blocks


BLOCKS = parse_blocks(README.read_text())


def free_port() -> int:
    with socket.socket() as s:  # bind, not connect: the suite's guard only blocks connect
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_the_readme_marks_every_kind_of_block_the_runner_understands() -> None:
    kinds = [b.kind for b in BLOCKS]
    for needed in ("file", "shell", "serve", "python", "library"):
        assert needed in kinds, f"README lost its `doctest: {needed}` block"
    assert set(kinds) <= {"file", "shell", "serve", "python", "library"}


class Stack:
    """The scratch directory, the environment and the background processes of one run."""

    def __init__(self, tmp: Path) -> None:
        self.dir = tmp / "work"
        self.dir.mkdir()
        shim = tmp / "bin"
        shim.mkdir()
        (shim / "spendgate").write_text(SHIM.format(python=sys.executable))
        (shim / "spendgate").chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")} | {
            "PATH": f"{shim}:{os.environ['PATH']}",
            "NO_PROXY": "127.0.0.1",
            "HOME": str(tmp),
        }
        self.fake_port, self.port = free_port(), free_port()
        self.procs: list[subprocess.Popen[str]] = []
        self.secret = ""

    def _spawn(
        self, argv: list[str], *, cwd: Path, env: dict[str, str], stderr: int
    ) -> subprocess.Popen[str]:
        proc = subprocess.Popen(  # noqa: S603 - argv built here, no shell from README text
            argv, text=True, start_new_session=True, cwd=cwd, env=env,
            stdout=subprocess.DEVNULL, stderr=stderr,
        )  # fmt: skip
        self.procs.append(proc)
        return proc

    def start_fake(self) -> None:
        env = self.env | {"PYTHONPATH": str(REPO)}
        self._spawn(
            [sys.executable, "-c", FAKE_SERVER, str(self.fake_port)],
            cwd=REPO, env=env, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        self.wait(f"http://127.0.0.1:{self.fake_port}/")

    def wait(self, url: str) -> None:
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-c", WAIT_READY, url], check=False, timeout=60
        )
        assert done.returncode == 0, f"{url} never answered"

    def adapt(self, text: str) -> str:
        return text.replace(REAL_UPSTREAM, f"http://127.0.0.1:{self.fake_port}/v1").replace(
            README_PORT, f"127.0.0.1:{self.port}"
        )

    def shell(self, script: str) -> str:
        """Run a block; keep the variables it exported. Returns its stdout."""
        dump = self.dir / "env.dump"
        full = f"set -e\n{script}\nenv -0 > {dump}\n"
        done = subprocess.run(  # noqa: S603
            [BASH, "-c", full], cwd=self.dir, env=self.env, capture_output=True, text=True,
            timeout=120, check=False,
        )  # fmt: skip
        assert done.returncode == 0, f"README shell block failed:\n{script}\n{done.stderr}"
        if found := KEY_LINE.search(done.stderr):  # `migrate --bootstrap` prints it once (SR-08)
            self.secret = found.group(1)
        self.env = dict(item.split("=", 1) for item in dump.read_text().split("\0") if "=" in item)
        return done.stdout

    def serve(self, script: str) -> None:
        commands = "\n".join(
            line for line in script.splitlines() if line.strip() and not line.startswith("#")
        )
        proc = self._spawn(
            [BASH, "-c", f"exec {commands}"],
            cwd=self.dir, env=self.env, stderr=subprocess.PIPE,
        )  # fmt: skip
        assert proc.stderr is not None

        def pump() -> None:  # keep draining stderr so the server never blocks on a full pipe
            assert proc.stderr is not None
            for line in proc.stderr:
                assert not KEY_LINE.search(line), "serve printed a key secret (SR-08)"

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        self.wait(f"http://127.0.0.1:{self.port}/livez")
        self.reader = reader

    def python(self, code: str) -> str:
        code = code.replace(PLACEHOLDER_KEY, self.secret)
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code], cwd=self.dir, env=self.env, capture_output=True,
            text=True, timeout=120, check=False,
        )  # fmt: skip
        assert done.returncode == 0, f"README python block failed:\n{done.stderr}"
        return done.stdout

    def stop(self) -> None:
        for proc in reversed(self.procs):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
        for proc in self.procs:
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            if proc.stderr is not None:
                proc.stderr.close()
        if hasattr(self, "reader"):
            self.reader.join(timeout=5)


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[Stack]:
    s = Stack(tmp_path)
    try:
        yield s
    finally:
        s.stop()


def test_proxy_quickstart_runs_end_to_end(stack: Stack) -> None:
    stack.start_fake()
    outputs: list[str] = []
    for block in BLOCKS:
        match block.kind:
            case "file":
                assert block.arg is not None
                (stack.dir / block.arg).write_text(stack.adapt(block.text))
            case "shell":
                outputs.append(stack.shell(block.text))
            case "serve":
                stack.serve(block.text)
            case "python":
                outputs.append(stack.python(stack.adapt(block.text)))
    # The python block printed the model's reply; the usage block shows the money it settled.
    assert outputs[-2].strip(), "the client block printed nothing"
    assert "gpt-6-luna" in outputs[-1], f"usage did not report the call:\n{outputs[-1]}"


def test_library_quickstart_runs_against_the_fake_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeUpstream()
    made: list[spendgate.local.Guard] = []
    real = spendgate.local.guard

    def guard(*args: object, **kwargs: object) -> spendgate.local.Guard:
        g = real(*args, client=fake.client(), now_ms=lambda: NOW_MS, **kwargs)  # type: ignore[arg-type]
        made.append(g)
        return g

    monkeypatch.setattr(spendgate.local, "guard", guard)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")  # pragma: allowlist secret
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path))
    (block,) = (b for b in BLOCKS if b.kind == "library")
    try:
        exec(compile(block.text, "README.md (library)", "exec"), {"__name__": "readme"})  # noqa: S102
    finally:
        for g in made:
            g.close()
    assert made, "the library block never called spendgate.local.guard"
    assert [r.path for r in fake.requests] == ["/v1/chat/completions"]
