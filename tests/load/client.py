"""One client process: `concurrency` coroutines calling the gateways until `calls` are done.

Run: `python -m tests.load.client '<json spec>'`; prints one JSON document on stdout. Every random
choice (stream or not, latency, 429, cut, cancel, key) comes from `random.Random(seed)`, so one
client's schedule is reproducible; how the processes interleave is up to the kernel.
"""

import asyncio
import json
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from tests.load.plan import Plan

PROMPT = "Summarise the quarterly report in one sentence."


@dataclass(slots=True)
class Mix:
    """Probabilities per call (the rest are plain 200 replies)."""

    stream: float = 0.4
    throttle: float = 0.08  # the provider answers 429
    server_error: float = 0.02  # the provider answers 500
    cut: float = 0.04  # of streams: the connection drops mid-stream
    cancel: float = 0.05  # the client goes away before the answer
    overreport: float = 0.0  # the provider reports 3x the output tokens
    max_latency_ms: int = 60
    backoff_ms: int = 1000  # pause after a budget refusal (a client told "exhausted" waits a bit)


def make_plan(
    rng: random.Random, mix: Mix, key: int, max_tokens: int
) -> tuple[Plan, bool, int | None]:
    """(provider plan, stream?, cancel-after ms or None) for one call."""
    stream = rng.random() < mix.stream
    roll = rng.random()
    status = 200
    if roll < mix.throttle:
        status = 429
    elif roll < mix.throttle + mix.server_error:
        status = 500
    # Output near the ceiling, so settled spend is close to the hold and budgets really fill up.
    out = rng.randint(int(max_tokens * 0.7), max_tokens)
    # Heavy-ish tail: most calls are quick, a few are slow.
    latency = int(rng.expovariate(1 / (mix.max_latency_ms / 4)))
    plan = Plan(
        key=key,
        latency_ms=min(latency, mix.max_latency_ms),
        out_tokens=out,
        status=status,
        cut_after=rng.randint(1, 2) if stream and rng.random() < mix.cut else None,
        overreport=rng.random() < mix.overreport,
    )
    cancel = rng.randint(1, mix.max_latency_ms) if rng.random() < mix.cancel else None
    return plan, stream, cancel


def body_of(model: str, max_tokens: int, plan: Plan, *, stream: bool) -> dict[str, object]:
    body: dict[str, object] = {
        "model": model, "max_tokens": max_tokens, "user": plan.encode(),
        "messages": [{"role": "user", "content": PROMPT}],
    }  # fmt: skip
    if stream:
        body["stream"] = True
    return body


async def send(
    http: httpx.AsyncClient, url: str, secret: str, body: dict[str, object]
) -> tuple[int, str | None]:
    """(status, error code or None); a stream is read to its end."""
    headers = {"Authorization": f"Bearer {secret}"}
    async with http.stream("POST", url, json=body, headers=headers) as response:
        data = await response.aread()
    code = None
    if response.status_code >= 400:
        try:
            code = (json.loads(data).get("error") or {}).get("code")
        except ValueError:
            code = "unparsable"
    return response.status_code, code


async def run(spec: dict[str, object]) -> dict[str, object]:
    rng = random.Random(int(spec["seed"]))  # type: ignore[call-overload]  # noqa: S311
    mix = Mix(**spec.get("mix", {}))  # type: ignore[arg-type]
    urls = [f"http://127.0.0.1:{p}/v1/chat/completions" for p in spec["ports"]]  # type: ignore[attr-defined]
    keys: list[str] = spec["keys"]  # type: ignore[assignment]
    max_tokens = int(spec["max_tokens"])  # type: ignore[call-overload]
    todo = int(spec["calls"])  # type: ignore[call-overload]
    model = str(spec["model"])
    stop = (
        str(spec["stop_file"]) if spec.get("stop_file") else None
    )  # the driver creates it when every budget is exhausted
    labels: list[int] | None = spec.get("labels")  # type: ignore[assignment]
    records: list[list[object]] = []
    state = {"next": 0}
    limits = httpx.Limits(max_connections=int(spec["concurrency"]) + 4)  # type: ignore[call-overload]

    async def worker(http: httpx.AsyncClient) -> None:
        while state["next"] < todo and not (stop and Path(stop).exists()):
            n = state["next"] = state["next"] + 1
            key = rng.randrange(len(keys))
            label = labels[key] if labels else key  # what the fake counts the upstream call under
            plan, stream, cancel = make_plan(rng, mix, label, max_tokens)
            url = urls[n % len(urls)]
            t0 = time.perf_counter()
            status, code, how = 0, None, "ok"
            try:
                call = send(http, url, keys[key], body_of(model, max_tokens, plan, stream=stream))
                status, code = await asyncio.wait_for(call, cancel / 1000 if cancel else None)
            except TimeoutError:
                how = "cancelled"
            except httpx.RemoteProtocolError:
                how = "cut"
            except httpx.HTTPError as exc:
                how = f"error:{type(exc).__name__}"
            ms = (time.perf_counter() - t0) * 1000
            if code == "budget_exhausted" and mix.backoff_ms:
                await asyncio.sleep(rng.uniform(0.5, 1.5) * mix.backoff_ms / 1000)
            records.append(
                [
                    key,
                    status,
                    code,
                    how,
                    round(ms, 2),
                    plan.latency_ms,
                    stream,
                    plan.status,
                    plan.cut_after,
                    plan.overreport,
                ]
            )

    async with httpx.AsyncClient(limits=limits, timeout=30.0) as http:
        await asyncio.gather(*(worker(http) for _ in range(int(spec["concurrency"]))))  # type: ignore[call-overload]
    return {"records": records}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(json.loads(sys.argv[1])))))
