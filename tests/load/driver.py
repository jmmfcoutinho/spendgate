"""Orchestrates one load run, writes a JSON report: `python -m tests.load.driver CFG OUT`.

Two modes (`cfg["mode"]`): `load` (overshoot) and `overhead`. The pytest side only starts this
process and asserts on the report; sockets live here, outside the pytest socket guard.
"""

import asyncio
import json
import os
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.conninfo import make_conninfo

from spendgate.core.budgets import Period, ScopeKind
from spendgate.ledger.key_types import KeySpec
from tests.load import checks
from tests.load.client import PROMPT
from tests.load.facts import pending_by_owner, read_facts
from tests.load.plan import Plan
from tests.load.rig import (
    MODEL,
    ROOT,
    Leaf,
    Rig,
    add_user,
    build_world,
    now_ms,
    open_runtime_for,
    usd,
)

MAX_TOKENS = 100
CLIENT_ARGV = [sys.executable, "-m", "tests.load.client"]


def location_of(rig: Rig) -> str:
    return str(rig.workdir / "ledger.db") if rig.backend == "sqlite" else str(rig.dsn)


def start_clients(rig: Rig, specs: list[dict[str, Any]]) -> list[subprocess.Popen[bytes]]:
    """One client process per spec, all at once."""
    return [
        subprocess.Popen(  # noqa: S603
            [*CLIENT_ARGV, json.dumps(spec)], cwd=ROOT, env=rig.env, stdout=subprocess.PIPE
        )
        for spec in specs
    ]


def collect(procs: list[subprocess.Popen[bytes]]) -> list[list[list[Any]]]:
    out: list[list[list[Any]]] = []
    for proc in procs:
        stdout, _ = proc.communicate(timeout=1800)
        if proc.returncode != 0:
            raise RuntimeError(f"a client process failed with {proc.returncode}")
        out.append(json.loads(stdout)["records"])
    return out


def run_clients(rig: Rig, specs: list[dict[str, Any]]) -> list[list[list[Any]]]:
    return collect(start_clients(rig, specs))


def client_spec(
    rig: Rig, cfg: dict[str, Any], n: int, keys: list[str], **extra: object
) -> dict[str, Any]:
    base = {
        "seed": cfg["seed"] * 1000 + n, "ports": rig.live_ports(), "keys": keys,
        "max_tokens": MAX_TOKENS, "model": MODEL, "concurrency": cfg["concurrency"],
        "calls": cfg["calls_per_client"], "mix": cfg.get("mix", {}),
    }  # fmt: skip
    return base | extra


async def fake_stats(rig: Rig) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as http:
        reply = await http.get(f"http://127.0.0.1:{rig.fake_port}/_load/stats")
        stats: dict[str, Any] = reply.json()
        return stats


async def probe_worst_case(rig: Rig) -> int:
    """One real call on a throwaway budget: the hold the gateway takes for the standard request."""
    async with open_runtime_for(rig) as rt:
        now = now_ms()
        org = await rt.ledger.create_scope(ScopeKind.ORG, "probe", None, now=now)
        await rt.ledger.create_budget(org.id, Period.NONE, usd("1000"), now=now)
        user = await rt.ledger.create_scope(ScopeKind.USER, "probe-user", org.id, now=now)
        made = await rt.keys.create_key(KeySpec(user.id, "probe"), actor="load", now=now)
    body = {"model": MODEL, "max_tokens": MAX_TOKENS, "user": Plan(key=99).encode(),
            "messages": [{"role": "user", "content": PROMPT}]}  # fmt: skip
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(
            f"http://127.0.0.1:{rig.ports[0]}/v1/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {made.secret}"},
        )
    assert r.status_code == 200, r.text
    items = read_facts(rig.backend, location_of(rig))["items"]
    return int(items[0][3]) // int(items[0][2])


def exhausted_keys(facts: dict[str, Any], leaves: list[Leaf], *, held: bool) -> list[int]:
    """Keys whose chain cannot admit even the smallest hold seen (or is held for review).

    `held=False` counts only booked spend, which never goes down: once true, always true.
    """
    by_id = {b["id"]: b for b in facts["budgets"]}
    smallest = facts["hold_range"][0] or 0
    out = []
    for leaf in leaves:
        for bid in leaf.budget_ids:
            b = by_id[bid]
            used = b["spent"] + (b["held"] if held else 0)
            if b["review_at"] is not None or b["cap"] - used < smallest:
                out.append(leaf.index)
                break
    return out


async def watch_exhaustion(  # noqa: PLR0913, PLR0917 - one watcher, named parts
    rig: Rig, leaves: list[Leaf], procs: list[subprocess.Popen[bytes]], stop: Path, seconds: float,
    audit: dict[str, Any],
) -> str:  # fmt: skip
    """Tell the clients to stop once every key is exhausted (or after `seconds`); why it ended."""
    deadline = time.monotonic() + seconds
    why = "timeout"
    while time.monotonic() < deadline and any(p.poll() is None for p in procs):
        await asyncio.sleep(0.25)
        facts = read_facts(rig.backend, location_of(rig))
        checks.audit_snapshot(facts, audit)
        n_dead = len(exhausted_keys(facts, leaves, held=False))
        if n_dead == len(leaves):
            why = "exhausted"
            break
    else:
        why = (
            "clients_done" if why == "timeout" and not any(p.poll() is None for p in procs) else why
        )
    stop.touch()
    return why


async def wait_pending_zero(rig: Rig, seconds: float = 20.0) -> int:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pending = read_facts(rig.backend, location_of(rig))["pending"]
        if pending == 0:
            return 0
        await asyncio.sleep(0.5)
    return int(read_facts(rig.backend, location_of(rig))["pending"])


async def sweep_at_end(rig: Rig, lease_s: int = 60, settle_s: float = 10.0) -> int:
    """Run the sweeper as the server does, with a clock past every lease: what it has to catch.

    The sweep skips rows another session has locked, as the server's does; a killed gateway's
    Postgres session can hold such a lock for a moment after it dies, so pending holds are swept
    again for up to `settle_s`, as the server's next pass would.
    """
    deadline, total = time.monotonic() + settle_s, 0
    async with open_runtime_for(rig) as rt:
        while True:
            result = await rt.ledger.sweep_expired(now_ms() + (lease_s + 5) * 1000, 100)
            total += len(result.expired)
            if len(result.expired) == 100:
                continue
            if not read_facts(rig.backend, location_of(rig))["pending"]:
                return total
            if time.monotonic() > deadline:
                return total
            await asyncio.sleep(0.5)


async def limiter_phase(rig: Rig, cfg: dict[str, Any]) -> dict[str, Any]:
    """Per-process limits across workers (an rpm key and a concurrency key)."""
    out: dict[str, Any] = {"workers": len(rig.ports)}
    async with open_runtime_for(rig) as rt:
        now = now_ms()
        org = await rt.ledger.create_scope(ScopeKind.ORG, "limits", None, now=now)
        await rt.ledger.create_budget(org.id, Period.NONE, usd("1000"), now=now)
        rpm = await add_user(rt, org, "rpm", usd("500"), 0, (), rpm=cfg["rpm_limit"])
        conc = await add_user(rt, org, "conc", usd("500"), 0, (), max_concurrent=cfg["conc_limit"])
    quiet = {"stream": 0, "throttle": 0, "server_error": 0, "cut": 0, "cancel": 0}
    for name, leaf, calls, extra in (
        ("rpm", rpm, cfg["rpm_limit"] * 6 * len(rig.ports), {"max_latency_ms": 1}),
        ("concurrency", conc, 64, {"max_latency_ms": 600}),
    ):
        spec = client_spec(
            rig, cfg, 0, [leaf.secret], calls=calls, concurrency=min(calls, 64), labels=[98]
        )
        spec["mix"] = quiet | extra
        started = time.monotonic()
        records = run_clients(rig, [spec])[0]
        codes: dict[str, int] = {}
        for r in records:
            codes[str(r[2] or r[1])] = codes.get(str(r[2] or r[1]), 0) + 1
        out[name] = {"calls": len(records), "seconds": round(time.monotonic() - started, 2),
                     "outcomes": codes}  # fmt: skip
    out["rpm_limit_per_key"] = cfg["rpm_limit"]
    out["concurrency_limit_per_key"] = cfg["conc_limit"]
    return out


def read_logs(rig: Rig) -> dict[str, Any]:
    """Error-level lines and tracebacks (by exception type) in the gateway logs."""
    found: dict[str, Any] = {"error_lines": 0, "tracebacks": {}}
    for n in range(len(rig.gateways)):
        lines = (rig.workdir / f"gw{n}.log").read_text(errors="replace").splitlines()
        for i, line in enumerate(lines):
            if '"level": "error"' in line or '"level":"error"' in line:
                found["error_lines"] += 1
            if line.startswith("Traceback"):
                end = next((j for j in range(i + 1, len(lines)) if lines[j][:1] not in " T"), i)
                kind = lines[end].split(":")[0]
                found["tracebacks"][kind] = found["tracebacks"].get(kind, 0) + 1
    return found


VICTIM_HOLDS = 5  # pending holds of its own the killed gateway must have: what the sweeper ends


def owner_of(owners: dict[str, list[str]], pid: int) -> str | None:
    """The lease-owner name of the gateway process `pid` (`pipeline.instance_id`: host-pid-tail),
    or None while it owns no pending hold. A name of another form fails the run at once: matching
    by pid would silently find nothing, and the crash check would blame the sweeper."""
    for owner in owners:
        fields = owner.rsplit("-", 2)
        if len(fields) != 3 or not fields[1].isdigit():
            raise RuntimeError(f"owner_instance {owner!r} is not host-pid-tail (see instance_id)")
        if fields[1] == str(pid):
            return owner
    return None


async def kill_gateway_later(rig: Rig, seconds: float) -> dict[str, Any]:
    """kill -9 the first gateway mid-run: its in-flight holds can only be ended by the sweeper.

    It waits for pending holds of the victim itself: a count over all gateways can be reached
    while the victim owns none, and then the run exercises no sweep at all. The count is taken
    with the victim paused (SIGSTOP), so its holds cannot settle between the count and the kill:
    a fast ledger settles a short call in milliseconds. A transaction it had open when paused is
    rolled back when it dies, which leaves that hold pending. What the victim left pending is read
    once it is dead, so nothing it did can change the list afterwards.
    """
    await asyncio.sleep(seconds)
    victim = rig.gateways[0]
    owner = None
    for _ in range(600):
        victim.send_signal(signal.SIGSTOP)
        owners = pending_by_owner(rig.backend, location_of(rig))
        owner = owner_of(owners, victim.pid) or owner
        if owner and len(owners.get(owner, [])) >= VICTIM_HOLDS:
            break
        victim.send_signal(signal.SIGCONT)
        await asyncio.sleep(0.05)
    victim.kill()  # SIGKILL ends a paused process too
    victim.wait()
    owners = pending_by_owner(rig.backend, location_of(rig))
    owner = owner_of(owners, victim.pid) or owner
    if owner is None:
        raise RuntimeError(f"no pending hold carries the killed gateway's pid {victim.pid}")
    return {"owner": owner, "pending": owners.get(owner, [])}


async def run_load(rig: Rig, cfg: dict[str, Any]) -> dict[str, Any]:
    rig.lease_s = cfg.get("lease_seconds", 60)
    await rig.start_fake()
    async with open_runtime_for(rig):  # migrates before several gateways open the ledger at once
        pass
    await rig.start_gateways(cfg["gateways"])
    worst = await probe_worst_case(rig)
    s = cfg["scale"]
    async with open_runtime_for(rig) as rt:
        leaves = await build_world(
            rt, w_nano=worst, org_calls=160 * s, project_calls=100 * s, user_calls=35 * s
        )
    keys = [leaf.secret for leaf in leaves]
    before = await fake_stats(rig)
    t0 = time.monotonic()
    stop = rig.workdir / "stop"
    specs = [client_spec(rig, cfg, n, keys, stop_file=str(stop)) for n in range(cfg["clients"])]
    procs = start_clients(rig, specs)
    killer = (
        asyncio.create_task(kill_gateway_later(rig, cfg["crash_after_s"]))
        if cfg.get("crash_after_s")
        else None
    )
    audit: dict[str, Any] = {"snapshots": 0, "violations": [], "peak_overshoot_nano": 0}
    ended = await watch_exhaustion(rig, leaves, procs, stop, cfg["max_seconds"], audit)
    records = [r for chunk in collect(procs) for r in chunk]
    victim = await killer if killer else None
    seconds = time.monotonic() - t0
    pending_after_run = await wait_pending_zero(rig)
    mid = read_facts(rig.backend, location_of(rig))
    stats_mid = await fake_stats(rig)
    dead = exhausted_keys(mid, leaves, held=True)
    # Volley: every key whose chain is exhausted keeps calling; none may reach the provider.
    volley_specs = [
        client_spec(
            rig,
            cfg,
            100 + k,
            [keys[k]],
            labels=[k],
            calls=cfg["volley_calls"],
            concurrency=8,
            mix={"cancel": 0, "backoff_ms": 0},
        )
        for k in dead
    ]
    volley = run_clients(rig, volley_specs) if volley_specs else []
    stats_end = await fake_stats(rig)
    limiter = None if killer else await limiter_phase(rig, cfg)
    pending_final = await wait_pending_zero(rig)
    swept = await sweep_at_end(rig, rig.lease_s)
    final = read_facts(rig.backend, location_of(rig))
    return {
        "worst_nano": worst, "seconds": round(seconds, 2), "ended": ended, "audit": audit,
        "records": records,
        "volley": [[r[1:4] for r in chunk] for chunk in volley], "volley_keys": dead,
        "leaves": [{"index": x.index, "budgets": list(x.budget_ids)} for x in leaves],
        "stats_before": before, "stats_mid": stats_mid, "stats_end": stats_end,
        "facts_mid": mid, "facts": final, "swept": swept, "pending_after_run": pending_after_run,
        "pending_final": pending_final, "limiter": limiter, "crashed": killer is not None,
        "victim": victim,
    }  # fmt: skip


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


async def run_overhead(rig: Rig, cfg: dict[str, Any]) -> dict[str, Any]:
    """Alternate one direct call to the fake and one call through the gateway, at 0 ms latency."""
    await rig.start_fake()
    async with open_runtime_for(rig):
        pass
    await rig.start_gateways(1)
    async with open_runtime_for(rig) as rt:
        now = now_ms()
        org = await rt.ledger.create_scope(ScopeKind.ORG, "over", None, now=now)
        await rt.ledger.create_budget(org.id, Period.NONE, usd("100000"), now=now)
        leaf = await add_user(rt, org, "u", usd("100000"), 0, ())
    results: dict[str, Any] = {}
    gateway = f"http://127.0.0.1:{rig.ports[0]}/v1/chat/completions"
    direct = f"http://127.0.0.1:{rig.fake_port}/v1/chat/completions"
    auth = {"Authorization": f"Bearer {leaf.secret}"}
    async with httpx.AsyncClient(timeout=30) as http:
        for label, stream in (("plain", False), ("stream", True)):
            body: dict[str, Any] = {"model": MODEL, "max_tokens": MAX_TOKENS,
                "user": Plan(latency_ms=0, out_tokens=50).encode(),
                "messages": [{"role": "user", "content": PROMPT}]}  # fmt: skip
            if stream:
                body["stream"] = True
            gw: list[float] = []
            dr: list[float] = []
            for i in range(cfg["warmup"] + cfg["samples"]):
                order = (
                    (("d", direct), ("g", gateway))
                    if i % 2 == 0
                    else (("g", gateway), ("d", direct))
                )
                for kind, url in order:
                    t = time.perf_counter()
                    r = await http.post(url, json=body, headers=auth if kind == "g" else {})
                    ms = (time.perf_counter() - t) * 1000
                    assert r.status_code == 200, r.text
                    if i >= cfg["warmup"]:
                        (gw if kind == "g" else dr).append(ms)
            base = statistics.median(dr)
            paired = [g - d for g, d in zip(gw, dr, strict=True)]
            results[label] = {
                "samples": len(gw), "direct_p50": pct(dr, 0.5), "direct_p99": pct(dr, 0.99),
                "gateway_p50": pct(gw, 0.5), "gateway_p99": pct(gw, 0.99),
                "overhead_p50": pct([g - base for g in gw], 0.5),
                "overhead_p99": pct([g - base for g in gw], 0.99),
                "paired_p50": pct(paired, 0.5), "paired_p99": pct(paired, 0.99),
            }  # fmt: skip
    return results


def postgres_dsn(url: str, name: str) -> tuple[str, str]:
    schema = "sgload_" + name
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    return make_conninfo(url, options=f"-c search_path={schema}"), schema


async def main(cfg_path: str, out_path: str) -> None:
    cfg = json.loads(Path(cfg_path).read_text())
    schema = None
    with tempfile.TemporaryDirectory(prefix="sg-load-") as tmp:
        dsn = None
        if cfg["backend"] == "postgres":
            dsn, schema = postgres_dsn(
                cfg["postgres_url"], Path(tmp).name.replace("-", "_").lower()
            )
        rig = Rig(Path(tmp), cfg["backend"], dsn)
        os.environ.update(rig.env)  # this process reads the same config the gateways do
        try:
            result = await (run_load if cfg["mode"] == "load" else run_overhead)(rig, cfg)
        finally:
            rig.stop_all()
            if schema:
                with psycopg.connect(cfg["postgres_url"], autocommit=True) as conn:
                    conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        if cfg["mode"] == "load":
            result["gateway_logs"] = read_logs(rig)  # after the stop: logs are flushed
    Path(out_path).write_text(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
