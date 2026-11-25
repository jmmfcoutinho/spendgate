"""Processes and ledger world for the load tests: a fake provider, N gateways, a seeded ledger.

Everything here runs in the driver process (`tests.load.driver`), never inside pytest, so the
socket guard does not apply: the driver talks only to 127.0.0.1 and the pytest process only
reads the JSON report. The gateways are the real `spendgate serve` command.
"""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from spendgate.cli.runtime import Runtime, open_runtime
from spendgate.cli.serve import load_settings
from spendgate.config import Settings
from spendgate.core.budgets import Period, ScopeKind
from spendgate.core.money import Money
from spendgate.ledger.key_types import KeySpec
from spendgate.ledger.port import ScopeRow

ROOT = Path(__file__).resolve().parents[2]
PEPPER = "load-test-pepper-0123456789abcdef"
MODEL = "alpha"
GATEWAY_LAUNCH = "import sys; from spendgate.cli import main; sys.exit(main())"
CONFIG = """\
server: {{listen: "127.0.0.1:{port}", key_pepper: "env:LOAD_PEPPER", max_concurrent_requests: 2048}}
ledger:
  currency: USD
  backend: {backend}
  lease_seconds: {lease}
  heartbeat_seconds: {heartbeat}
  sweep_seconds: {sweep}
providers:
  openai: {{base_url: "http://127.0.0.1:{fake_port}", credential: "env:OPENAI_KEY"}}
routing: {{max_attempts: 3, cooldown_seconds: 1}}
# The overshoot scenarios need every caller in flight at once against the shared caps, so the rig
# removes the default per-key concurrency limit (16) on purpose instead of letting it refuse them.
defaults: {{limits: {{concurrency: null}}}}
models:
  {model}:
    catalog_id: openai/gpt-6-luna
    deployments:
      - {{provider: openai, base_url: "http://127.0.0.1:{fake_port}/a"}}
      - {{provider: openai, base_url: "http://127.0.0.1:{fake_port}/b"}}
      - {{provider: openai, base_url: "http://127.0.0.1:{fake_port}/c"}}
"""


def usd(amount: str) -> int:
    return Money.from_amount(amount, "USD").nano


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(slots=True)
class Rig:
    """Owns the temp directory, the child processes and the config files of one run."""

    workdir: Path
    backend: str  # "sqlite" | "postgres"
    dsn: str | None = None  # postgres only: a conninfo with its own search_path
    fake_port: int = field(default_factory=free_port)
    ports: list[int] = field(default_factory=list)
    procs: list[subprocess.Popen[bytes]] = field(default_factory=list)
    configs: list[Path] = field(default_factory=list)
    gateways: list[subprocess.Popen[bytes]] = field(default_factory=list)
    lease_s: int = 60  # crash runs shorten it so an expired hold shows up within the run

    @property
    def env(self) -> dict[str, str]:
        env = dict(os.environ) | {
            "LOAD_PEPPER": PEPPER,
            "OPENAI_KEY": "sk-load-not-real",
        }  # pragma: allowlist secret
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        if self.dsn:
            env["LOAD_DSN"] = self.dsn
        return env

    def _spawn(self, argv: list[str], log: str) -> subprocess.Popen[bytes]:
        out = (self.workdir / f"{log}.log").open("wb")
        proc = subprocess.Popen(argv, cwd=ROOT, env=self.env, stdout=out, stderr=subprocess.STDOUT)  # noqa: S603
        out.close()
        self.procs.append(proc)
        return proc

    def write_config(self, port: int, name: str) -> Path:
        if self.backend == "sqlite":
            backend = f'{{kind: sqlite, path: "{self.workdir / "ledger.db"}"}}'
        else:
            backend = '{kind: postgres, dsn: "env:LOAD_DSN"}'
        path = self.workdir / f"{name}.yaml"
        path.write_text(
            CONFIG.format(
                port=port,
                fake_port=self.fake_port,
                backend=backend,
                model=MODEL,
                lease=self.lease_s,
                heartbeat=max(1, self.lease_s // 4),
                sweep=max(1, min(30, self.lease_s // 4)),
            )
        )
        return path

    async def start_fake(self) -> None:
        self._spawn([sys.executable, "-m", "tests.load.fake_server", str(self.fake_port)], "fake")
        await self._wait(f"http://127.0.0.1:{self.fake_port}/_load/stats")

    async def start_gateways(self, count: int) -> None:
        for n in range(count):
            port = free_port()
            cfg = self.write_config(port, f"gw{n}")
            self.ports.append(port)
            self.configs.append(cfg)
            argv = [
                sys.executable,
                "-c",
                GATEWAY_LAUNCH,
                "serve",
                "--config",
                str(cfg),
                "--no-bootstrap",
            ]
            self.gateways.append(self._spawn(argv, f"gw{n}"))
        for port in self.ports:
            await self._wait(f"http://127.0.0.1:{port}/readyz")

    def _tail(self, proc: subprocess.Popen[bytes]) -> str:
        """The last log lines of a child that died, for the error message."""
        logs = sorted(self.workdir.glob("*.log"))
        index = self.procs.index(proc)
        names = ["fake", *[f"gw{n}" for n in range(len(self.gateways))]]
        log = self.workdir / f"{names[index]}.log" if index < len(names) else logs[0]
        return f"{proc.args!r}:\n" + "\n".join(log.read_text(errors="replace").splitlines()[-25:])

    async def _wait(self, url: str, seconds: float = 40.0) -> None:
        deadline = time.monotonic() + seconds
        async with httpx.AsyncClient(timeout=2.0) as http:
            while time.monotonic() < deadline:
                for proc in self.procs:
                    if proc.poll() is not None:
                        raise RuntimeError(f"a child process exited early: {self._tail(proc)}")
                try:
                    if (await http.get(url)).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
        raise RuntimeError(f"{url} did not become ready")

    def live_ports(self) -> list[int]:
        return [p for p, g in zip(self.ports, self.gateways, strict=True) if g.poll() is None]

    def stop_all(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in self.procs:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def settings(self) -> Settings:
        """Settings of the ledger (any gateway's config names the same one)."""
        cfg = self.write_config(0, "admin") if not self.configs else self.configs[0]
        loaded = load_settings(str(cfg))
        if isinstance(loaded, int):
            raise RuntimeError("the load config is invalid")
        return loaded


@dataclass(frozen=True, slots=True)
class Leaf:
    """One user key: the scope chain it spends against and its client secret."""

    index: int
    scope: ScopeRow
    secret: str
    budget_ids: tuple[str, ...]  # leaf first, org last


async def add_user(  # noqa: PLR0913, PLR0917 - one call builds one named leaf
    rt: Runtime, parent: ScopeRow, name: str, cap_nano: int, index: int, chain: tuple[str, ...],
    **limits: int,
) -> Leaf:  # fmt: skip
    now = now_ms()
    scope = await rt.ledger.create_scope(ScopeKind.USER, name, parent.id, now=now)
    budget = await rt.ledger.create_budget(scope.id, Period.NONE, cap_nano, now=now)
    made = await rt.keys.create_key(KeySpec(scope.id, name, **limits), actor="load", now=now)  # type: ignore[arg-type]
    return Leaf(index, scope, made.secret, (budget.id, *chain))


async def build_world(  # noqa: PLR0913
    rt: Runtime, *, w_nano: int, org_calls: int, project_calls: int, user_calls: int,
    projects: int = 2, users: int = 3,
) -> list[Leaf]:  # fmt: skip
    """org > projects > users, each cap sized in whole worst-case holds so the upper levels bind."""
    now = now_ms()
    org = await rt.ledger.create_scope(ScopeKind.ORG, "load", None, now=now)
    org_budget = await rt.ledger.create_budget(org.id, Period.NONE, org_calls * w_nano, now=now)
    leaves: list[Leaf] = []
    for p in range(projects):
        project = await rt.ledger.create_scope(ScopeKind.PROJECT, f"p{p}", org.id, now=now)
        pb = await rt.ledger.create_budget(project.id, Period.NONE, project_calls * w_nano, now=now)
        for u in range(users):
            leaf = await add_user(
                rt, project, f"u{p}{u}", user_calls * w_nano, len(leaves), (pb.id, org_budget.id)
            )
            leaves.append(leaf)
    return leaves


def open_runtime_for(rig: Rig) -> AbstractAsyncContextManager[Runtime]:
    return open_runtime(rig.settings())
