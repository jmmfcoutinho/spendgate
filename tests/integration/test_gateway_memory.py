"""16 parallel 25 MB uploads stay under the in-flight memory budget.

Measured with `tracemalloc` (peak above the start of the burst; the 24 MB request body is built
before measuring and shared by all 16 requests). One upload peaks at about 3 copies of its body
(read buffer, extracted file part, the body the adapter rebuilds), so `intake.COPIES = 3` and the
default gate budget is 384 MiB (SR-01: under the reference container's 512 MiB), five of these
uploads. With 16 arriving at once from 16 keys, 5 are admitted and 11 are refused at once with
429 `rate_limited`. One key alone is held to a quarter of the budget (NF-01), so it gets one such
upload at a time; hence the 16 keys.
"""

import asyncio
import tracemalloc
from pathlib import Path

import httpx
import pytest

from spendgate.ledger.key_types import KeySpec
from spendgate.pipeline import intake
from tests.integration.harness import NOW_MS, async_test, open_stack
from tests.unit.audio_builders import wav

PARALLEL = 16
MB = 1024 * 1024


def multipart(audio: bytes) -> tuple[bytes, str]:
    boundary = "memoryboundary"
    fields = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\nwhisper\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    )
    body = fields.encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


@pytest.mark.slow
@async_test
async def test_parallel_large_uploads_stay_under_the_memory_budget(tmp_path: Path) -> None:
    body, content_type = multipart(wav(780.0))  # about 25 MB, just under the 25 MB limit
    assert 23 * MB < len(body) < 25 * MB
    arrived = 0
    expected = 0  # how many uploads the gate admits; set once the stack exists

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal arrived
        arrived += 1  # hold every admitted upload in memory at once before any finishes
        while arrived < expected:
            await asyncio.sleep(0.005)
        usage = {"type": "duration", "seconds": 780}
        return httpx.Response(200, json={"text": "hi", "usage": usage})

    sender = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    async with open_stack(tmp_path, budget="100", pipeline_kwargs={"sender": sender}) as s:
        budget = s.pipeline.gate.memory_bytes  # the default gate budget (SR-01: under 512 MiB)
        expected = budget // (len(body) * intake.COPIES)
        secrets = [s.secret]
        for n in range(PARALLEL - 1):
            made = await s.keys.create_key(KeySpec(s.scope.id, f"k{n}"), actor="t", now=NOW_MS)
            secrets.append(made.secret)
        heads = [{"Authorization": f"Bearer {k}", "Content-Type": content_type} for k in secrets]
        tracemalloc.start()
        tracemalloc.reset_peak()
        try:
            responses = await asyncio.gather(
                *[s.client.post("/v1/audio/transcriptions", content=body, headers=h) for h in heads]
            )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        print(f"\npeak {peak / MB:.0f} MiB for {expected} admitted of {PARALLEL} uploads")
        statuses = sorted(r.status_code for r in responses)
        assert statuses == [200] * expected + [429] * (PARALLEL - expected)
        refused = [r for r in responses if r.status_code == 429]
        assert {r.json()["error"]["code"] for r in refused} == {"rate_limited"}
        assert peak < budget  # the accounting is conservative: real peak below the budget
        spent, held = await s.totals()
        assert held == 0 and spent > 0
        assert s.pipeline.gate.in_flight == 0  # every slot came back
    await sender.aclose()
