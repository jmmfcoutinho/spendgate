"""Only FINAL stream usage settles a stream (M-13, ledger-state-machine scenarios 3 and 17).

Gemini repeats running totals on every chunk; only the totals sent with (or after) the finish
reason are the answer's usage. A stream that is cut, cancelled by the client or stopped by the
gateway before them was charged the last partial total; it must be charged the full hold. An
OpenAI-compatible upstream that puts running totals on content chunks gets the same treatment.
"""

from pathlib import Path

from spendgate.core.cost import Usage, actual_cost
from spendgate.core.outcomes import Outcome
from spendgate.core.policy import KillSwitch, KillTarget
from tests.integration.harness import NOW_MS, Stack, async_test
from tests.integration.stream_rig import sse
from tests.integration.test_gateway_streaming import gated, open_call, settled_once

GEMINI_BODY: dict[str, object] = {
    "model": "gemini", "max_tokens": 4000, "stream": True,
    "messages": [{"role": "user", "content": "write a long essay"}],
}  # fmt: skip


def gemini(prompt: int, candidates: int, finish: str | None = None) -> bytes:
    """One `alt=sse` chunk carrying Gemini's running totals, optionally the finishing one."""
    candidate: dict[str, object] = {"content": {"role": "model", "parts": [{"text": "word "}]}}
    if finish is not None:
        candidate["finishReason"] = finish
    meta = {
        "promptTokenCount": prompt, "candidatesTokenCount": candidates,
        "totalTokenCount": prompt + candidates,
    }  # fmt: skip
    return sse({"candidates": [candidate], "usageMetadata": meta})


def gemini_cost(s: Stack, prompt: int, candidates: int) -> int:
    entry = s.settings.catalog.lookup("google", "gemini-4-carbon", NOW_MS)
    usage = Usage(input_tokens=prompt, output_tokens=candidates)
    return s.settings.catalog.in_ledger(actual_cost(entry, usage)).nano


def held(s: Stack) -> int:
    return s.observer.held[0].hold.nano


@async_test
async def test_m13_gemini_cut_after_a_running_total_is_charged_the_full_hold(
    tmp_path: Path,
) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s, GEMINI_BODY)
        assert call.status == 200
        up.streams[0].put(gemini(10, 1))  # a running total, not the answer's usage
        up.streams[0].cut()
        await call.read_all()
        await call.finish()
        assert gemini_cost(s, 10, 1) < held(s)  # the partial total would under-charge
        await settled_once(s, Outcome.STREAM_BROKEN, held(s))
        (row,) = await s.ledger.get_calls(s.observer.held[0].request_id)
        assert row.usage_cost_nano is None  # no final usage was seen


@async_test
async def test_m13_gemini_complete_stream_is_charged_the_exact_usage(tmp_path: Path) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s, GEMINI_BODY)
        up.streams[0].put(gemini(10, 1))
        up.streams[0].put(gemini(10, 7, "STOP"))  # the final totals ride the finish reason
        up.streams[0].end()
        text = (await call.read_all()).decode()
        assert '"finish_reason": "stop"' in text and text.endswith("data: [DONE]\n\n")
        await call.finish()
        await settled_once(s, Outcome.OK, gemini_cost(s, 10, 7))


@async_test
async def test_m13_gemini_client_cancel_after_a_running_total_is_charged_the_full_hold(
    tmp_path: Path,
) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s, GEMINI_BODY)
        up.streams[0].put(gemini(10, 1))
        assert await call.chunk() is not None  # the client saw the first words, then left
        await call.disconnect()
        assert up.streams[0].closed
        await settled_once(s, Outcome.CANCELLED, held(s))


@async_test
async def test_m13_gemini_gateway_stop_after_a_running_total_is_charged_the_full_hold(
    tmp_path: Path,
) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s, GEMINI_BODY)
        up.streams[0].put(gemini(10, 1))
        assert await call.chunk() is not None
        await s.ledger.engage_kill_switch(
            KillSwitch(KillTarget.GLOBAL, ""), actor="t", reason="test", now=NOW_MS
        )
        await s.pipeline.renew_leases()
        await s.pipeline.watch_streams()  # the heartbeat stops the stream
        assert "kill switch" in (await call.read_all()).decode()
        await call.finish()
        await settled_once(s, Outcome.STREAM_BROKEN, held(s))


@async_test
async def test_m13_openai_running_total_on_a_content_chunk_is_not_final_usage(
    tmp_path: Path,
) -> None:
    async with gated(tmp_path) as (s, up):
        call = await open_call(s)
        delta = {"index": 0, "delta": {"content": "Hi"}, "finish_reason": None}
        running = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        up.streams[0].put(sse({"id": "c", "choices": [delta], "usage": running}))
        up.streams[0].cut()
        await call.read_all()
        await call.finish()
        await settled_once(s, Outcome.STREAM_BROKEN, held(s))
