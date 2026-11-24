"""Body memory at intake: what a request body is charged against the process gate, and when.

A caller with no key never reads a byte; JSON has its own small cap because it is parsed into many
copies; a body is charged as its bytes arrive, and one key cannot take more than its share.
"""

import asyncio
import tracemalloc
from pathlib import Path

import pytest

from spendgate.core.errors import ErrorCode
from spendgate.pipeline import intake
from spendgate.pipeline.model import CHAT
from tests.integration.harness import async_test, open_stack
from tests.integration.uploads import MIB, Body, auth, bearer, read_or_refused, second_key


# T-DOS-01
@async_test
async def test_sr_01_an_unauthenticated_25_mib_json_body_is_refused_unread(tmp_path: Path) -> None:
    """SR-01: no key, no read and no gate place, however big the declared body."""
    async with open_stack(tmp_path) as s:
        body = Body([b"[" + b"{}," * 8 * MIB])  # about 25 MiB
        headers = {"content-type": "application/json", "content-length": str(25 * MIB)}
        reply = await s.pipeline.serve(CHAT, headers, body)
        assert reply.status == 401 and body.pulled == 0
        assert s.pipeline.gate.in_flight == 0
        s.assert_no_contact()


# T-DOS-01, T-DOS-03
@async_test
async def test_sr_01_json_has_its_own_small_cap_checked_before_the_read(tmp_path: Path) -> None:
    """SR-01: the JSON cap is checked on Content-Length before any read, and again while reading."""
    async with open_stack(tmp_path) as s:
        body = Body([b"x"])
        headers = bearer(s) | {"content-length": str(5 * MIB)}  # over 4 MiB, under 25 MiB
        reply = await s.pipeline.serve(CHAT, headers, body)
        assert reply.status == 413 and body.pulled == 0
        chunked = Body([b" " * (4 * MIB), b" "])  # no Content-Length: the read cap stops it
        assert (await s.pipeline.serve(CHAT, bearer(s), chunked)).status == 413


def test_sr_01_default_gate_memory_stays_under_the_reference_container_limit() -> None:
    """SR-01: the default gate fits a 512 MiB container."""
    assert intake.DEFAULT_MEMORY_BYTES <= 400 * MIB < 512 * MIB


@pytest.mark.slow
@async_test
async def test_sr_01_parallel_amplifying_json_bodies_stay_under_the_gate_budget(
    tmp_path: Path,
) -> None:
    """SR-01: eight near-cap JSON bodies parsed at once stay under the gate's memory budget."""
    head = b'{"model":"alpha","max_tokens":10,"messages":[{"role":"user","content":"hi"}],"x":['
    body = head + b"{}," * (4 * MIB // 3 - 100) + b"{}]}"
    assert len(body) < 4 * MIB
    async with open_stack(tmp_path) as s:
        budget = s.pipeline.gate.memory_bytes
        headers = {"Authorization": f"Bearer {s.secret}", "Content-Type": "application/json"}
        tracemalloc.start()
        tracemalloc.reset_peak()
        try:
            responses = await asyncio.gather(
                *[s.client.post(CHAT.path, content=body, headers=headers) for _ in range(8)]
            )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < budget
        assert {r.status_code for r in responses} <= {200, 400, 429}


def test_nf_01_an_honest_request_is_not_charged_the_worst_case_before_its_bytes_arrive() -> None:
    """NF-01: a body is charged as it arrives, not at the cap up front."""
    gate = intake.Gate(8, 4 * MIB, 384 * MIB)
    slot = gate.enter(None, max_body=4 * MIB, copies=intake.JSON_AMPLIFICATION, owner="k")
    assert isinstance(slot, intake.Slot)
    assert gate.buffered_bytes == 0  # nothing buffered yet, nothing charged (was 4 MiB x 32)
    assert slot.grow(1 * MIB) is None
    assert gate.buffered_bytes == MIB * intake.JSON_AMPLIFICATION
    slot.release()
    assert gate.buffered_bytes == 0 and gate.in_flight == 0


def test_nf_01_one_key_cannot_take_more_than_its_share_of_the_gate() -> None:
    """NF-01: a second upload of one key past its quarter is refused; other keys are untouched."""
    gate = intake.Gate(8, 4 * MIB, 384 * MIB)
    a = gate.enter(None, max_body=4 * MIB, copies=32, owner="a")
    b = gate.enter(None, max_body=4 * MIB, copies=32, owner="a")
    other = gate.enter(None, max_body=4 * MIB, copies=32, owner="b")
    assert isinstance(a, intake.Slot) and isinstance(b, intake.Slot)
    assert isinstance(other, intake.Slot)
    assert a.grow(3 * MIB) is None  # 96 MiB: the key's quarter
    refusal = b.grow(MIB)  # a second upload of the same key: over its share
    assert refusal is not None and refusal.code is ErrorCode.RATE_LIMITED
    assert other.grow(3 * MIB) is None  # another key is untouched
    assert gate.buffered_bytes == 2 * 96 * MIB


def test_nf_01_a_key_can_always_have_one_maximal_request() -> None:
    """NF-01: the share never refuses a key's only request, even one larger than the share."""
    gate = intake.Gate(8, 4 * MIB, 384 * MIB)
    slot = gate.enter(4 * MIB, max_body=4 * MIB, copies=32, owner="a")  # 128 MiB > a quarter
    assert isinstance(slot, intake.Slot)


@async_test
async def test_nf_01_three_chunked_json_uploads_no_longer_fill_the_gate(tmp_path: Path) -> None:
    """NF-01: three stalled chunked uploads are charged only their bytes; another key gets in."""
    async with open_stack(tmp_path, max_concurrent=16) as s:
        other = await second_key(s)
        bodies = [Body([b"{"], stall=True) for _ in range(3)]
        calls = [asyncio.ensure_future(s.pipeline.serve(CHAT, auth(s.secret), b)) for b in bodies]
        for call, body in zip(calls, bodies, strict=True):
            await read_or_refused(call, body)
        assert s.pipeline.gate.buffered_bytes < MIB  # the old charge was 3 x 128 MiB
        ok = await s.post("/v1/chat/completions", {
            "model": "alpha", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}],
        }, key=other)  # fmt: skip
        assert ok.status_code == 200
        for call in calls:
            call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)
