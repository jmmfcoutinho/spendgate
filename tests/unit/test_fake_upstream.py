"""Self-tests of the fake upstream: every scripted behaviour, per provider shape."""

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal

import httpx
import pytest

from tests.fakes import (
    ABSURD_USAGE,
    FakeUpstream,
    FakeUsage,
    RefusingTransport,
    Script,
)
from tests.fakes.frames import decode_eventstream

pytestmark = pytest.mark.anyio

type Target = tuple[str, dict[str, object]]

USAGE = FakeUsage(
    input_tokens=100,
    output_tokens=40,
    cached_input_tokens=20,
    cache_write_tokens=7,
    reasoning_tokens=9,
    audio_seconds=Decimal("2.5"),
    characters=12,
)
CHAT: Target = ("/v1/chat/completions", {"model": "m", "stream_options": {"include_usage": True}})
RESPONSES: Target = ("/openai/v1/responses", {"model": "m"})
MESSAGES: Target = ("/v1/messages", {"model": "m", "max_tokens": 5})
CONVERSE: Target = ("/model/eu.x/converse", {})
GEMINI: Target = ("/v1beta/models/g:generateContent", {})


async def post(
    fake: FakeUpstream, path: str, body: Mapping[str, object], *, stream: bool = False, **kw: str
) -> httpx.Response:
    payload = {**body, "stream": True} if stream else body
    async with fake.client() as client:
        return await client.post(path, json=payload, **kw)  # type: ignore[arg-type]


def at(data: object, *path: str | int) -> object:
    for key in path:
        data = data[key]  # type: ignore[index]
    return data


# --- non-streamed usage, per shape ---------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "path", "expected"),
    [
        (CHAT, ("usage", "prompt_tokens"), 100),
        (CHAT, ("usage", "prompt_tokens_details", "cached_tokens"), 20),
        (CHAT, ("usage", "completion_tokens_details", "reasoning_tokens"), 9),
        (RESPONSES, ("usage", "input_tokens_details", "cached_tokens"), 20),
        (RESPONSES, ("usage", "output_tokens"), 40),
        (("/v1/embeddings", {"model": "m"}), ("usage", "prompt_tokens"), 100),
        (("/v1/audio/transcriptions", {}), ("usage", "seconds"), 2.5),
        (MESSAGES, ("usage", "cache_creation_input_tokens"), 7),
        (MESSAGES, ("usage", "cache_read_input_tokens"), 20),
        (MESSAGES, ("usage", "output_tokens"), 40),
        (CONVERSE, ("usage", "inputTokens"), 100),
        (CONVERSE, ("usage", "cacheWriteInputTokens"), 7),
        (GEMINI, ("usageMetadata", "thoughtsTokenCount"), 9),
        (GEMINI, ("usageMetadata", "cachedContentTokenCount"), 20),
        (("/speechtotext/transcriptions:transcribe", {}), ("durationMilliseconds",), 2500),
    ],
)
async def test_non_streamed_usage_is_scripted(
    target: Target, path: tuple[str, ...], expected: object
) -> None:
    fake = FakeUpstream(Script(usage=USAGE))
    response = await post(fake, *target)
    assert response.status_code == 200
    assert at(response.json(), *path) == expected


async def test_speech_shapes_return_audio_bytes_and_billed_characters() -> None:
    fake = FakeUpstream(Script(usage=USAGE))
    openai = await post(fake, "/v1/audio/speech", {"model": "m"})
    eleven = await post(fake, "/v1/text-to-speech/voice123", {"text": "hi"})
    assert openai.headers["content-type"] == "audio/mpeg"
    assert eleven.headers["content-type"] == "audio/mpeg"
    assert eleven.headers["character-cost"] == "12"
    assert eleven.content.startswith(b"ID3")


async def test_azure_openai_paths_use_the_openai_shapes() -> None:
    fake = FakeUpstream(Script(usage=USAGE))
    path = "/openai/deployments/my-dep/chat/completions"
    response = await post(fake, path, {}, **{"params": "api-version=2025-01-01"})  # type: ignore[arg-type]
    assert response.json()["usage"]["prompt_tokens"] == 100
    assert fake.requests[0].query == {"api-version": "2025-01-01"}


async def test_no_usage_script_omits_usage_everywhere() -> None:
    fake = FakeUpstream(Script(final_usage=False))
    for path, body in (CHAT, RESPONSES, MESSAGES, CONVERSE, GEMINI):
        text = (await post(fake, path, body)).text
        assert "usage" not in text.lower()


async def test_unknown_path_is_404_and_still_logged() -> None:
    fake = FakeUpstream()
    assert (await post(fake, "/nope", {})).status_code == 404
    assert fake.requests[0].shape is None


# --- streams --------------------------------------------------------------------------------


def sse_events(text: str) -> list[tuple[str | None, str]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines.get("event"), lines["data"]))
    return events


async def test_chat_stream_ends_with_usage_chunk_then_done() -> None:
    fake = FakeUpstream(Script(usage=USAGE))
    response = await post(fake, *CHAT, stream=True)
    events = sse_events(response.text)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert events[-1][1] == "[DONE]"
    final = json.loads(events[-2][1])
    assert final["choices"] == [] and final["usage"]["completion_tokens"] == 40


async def test_chat_stream_has_no_usage_chunk_unless_requested() -> None:
    fake = FakeUpstream(Script(usage=USAGE))
    response = await post(fake, "/v1/chat/completions", {"model": "m"}, stream=True)
    assert "usage" not in response.text


async def test_chat_stream_without_final_usage_still_ends_with_done() -> None:
    fake = FakeUpstream(Script(final_usage=False))
    events = sse_events((await post(fake, *CHAT, stream=True)).text)
    assert events[-1][1] == "[DONE]" and "usage" not in "".join(d for _, d in events)


async def test_responses_stream_names_its_events_and_completes_with_usage() -> None:
    fake = FakeUpstream(Script(usage=USAGE))
    events = sse_events((await post(fake, *RESPONSES, stream=True)).text)
    assert next(name for name, _ in events) == "response.created"
    name, data = events[-1]
    assert name == "response.completed"
    assert json.loads(data)["response"]["usage"]["input_tokens"] == 100


async def test_anthropic_stream_event_names_and_usage_split() -> None:
    fake = FakeUpstream(Script(usage=USAGE))
    events = sse_events((await post(fake, *MESSAGES, stream=True)).text)
    names = [name for name, _ in events]
    assert names[0] == "message_start" and names[-2:] == ["message_delta", "message_stop"]
    assert {"content_block_start", "content_block_delta", "content_block_stop"} <= set(names)
    start = json.loads(events[0][1])["message"]["usage"]
    delta = json.loads(events[-2][1])["usage"]
    assert (start["input_tokens"], start["cache_read_input_tokens"]) == (100, 20)
    assert delta["output_tokens"] == 40


async def test_bedrock_converse_stream_is_real_eventstream_framing() -> None:
    fake = FakeUpstream(Script(usage=USAGE))
    response = await post(fake, "/model/eu.x/converse-stream", {})
    events = decode_eventstream(response.content)  # raises on a bad CRC
    assert next(name for name, _ in events) == "messageStart"
    name, payload = events[-1]
    assert name == "metadata" and at(payload, "usage", "outputTokens") == 40


async def test_bedrock_stream_without_usage_has_no_metadata_event() -> None:
    fake = FakeUpstream(Script(final_usage=False))
    events = decode_eventstream((await post(fake, "/model/x/converse-stream", {})).content)
    assert "metadata" not in [name for name, _ in events]


async def test_gemini_stream_as_sse_and_as_json_array() -> None:
    fake = FakeUpstream(Script(usage=USAGE))
    path = "/v1beta/models/g:streamGenerateContent"
    sse = await post(fake, path, {}, **{"params": "alt=sse"})  # type: ignore[arg-type]
    chunks = [json.loads(data) for _, data in sse_events(sse.text)]
    assert chunks[-1]["usageMetadata"]["candidatesTokenCount"] == 40
    array = (await post(fake, path, {})).json()
    assert isinstance(array, list) and array[-1]["usageMetadata"]["thoughtsTokenCount"] == 9


# --- status, headers, latency ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "marker"),
    [
        (CHAT, "rate_limit_error"),
        (MESSAGES, "rate_limit_error"),
        (CONVERSE, "message"),
        (GEMINI, "RESOURCE_EXHAUSTED"),
    ],
)
async def test_throttle_status_and_retry_after_per_shape(target: Target, marker: str) -> None:
    fake = FakeUpstream(Script(status=429, retry_after="7"))
    response = await post(fake, *target)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert marker in response.text


async def test_anthropic_overloaded_529_has_its_own_error_type() -> None:
    fake = FakeUpstream(Script(status=529))
    response = await post(fake, *MESSAGES)
    assert response.json()["error"]["type"] == "overloaded_error"


async def test_latency_goes_through_the_delay_hook_without_sleeping() -> None:
    fake = FakeUpstream(Script(latency_ms=1500, frame_latency_ms=200))
    await post(fake, *CHAT)
    await post(fake, *CHAT, stream=True)
    assert fake.delays[0] == 1.5
    assert fake.delays[1] == 1.5 and fake.delays.count(0.2) >= 3


async def test_injected_delay_hook_replaces_the_recorder() -> None:
    seen: list[float] = []

    async def hook(seconds: float) -> None:
        seen.append(seconds)

    fake = FakeUpstream(Script(latency_ms=250), delay=hook)
    await post(fake, *CHAT)
    assert seen == [0.25] and fake.delays == []


async def test_queue_scripts_run_in_order_then_default_returns() -> None:
    fake = FakeUpstream()
    fake.enqueue(Script(status=429), Script(status=500))
    codes = [(await post(fake, *CHAT)).status_code for _ in range(3)]
    assert codes == [429, 500, 200]


async def test_extra_response_headers_are_sent() -> None:
    fake = FakeUpstream(Script(headers=(("x-ratelimit-remaining", "0"),)))
    assert (await post(fake, *CHAT)).headers["x-ratelimit-remaining"] == "0"


# --- request log ----------------------------------------------------------------------------


async def test_request_log_records_what_the_upstream_received() -> None:
    fake = FakeUpstream()
    fake.assert_no_contact()
    await post(fake, *CHAT, headers={"x-api-key": "k"})  # type: ignore[arg-type]
    (seen,) = fake.requests
    assert (seen.method, seen.path, seen.shape) == ("POST", "/v1/chat/completions", "openai_chat")
    assert seen.json()["model"] == "m" and seen.headers["x-api-key"] == "k"
    with pytest.raises(AssertionError, match="contacted 1"):
        fake.assert_no_contact()


# --- faults ---------------------------------------------------------------------------------


async def test_refused_connection_raises_before_sending_and_logs_nothing() -> None:
    fake = FakeUpstream(Script(refuse_connection=True))
    with pytest.raises(httpx.ConnectError):
        await post(fake, *CHAT)
    fake.assert_no_contact()


async def test_refusing_transport_wraps_any_transport_then_lets_requests_through() -> None:
    fake = FakeUpstream()
    wrapper = RefusingTransport(fake.transport(), refusals=1)
    async with httpx.AsyncClient(transport=wrapper, base_url="https://x.test") as client:
        with pytest.raises(httpx.ConnectError):
            await client.post(CHAT[0], json={})
        assert (await client.post(CHAT[0], json={})).status_code == 200
    assert (wrapper.refused, fake.calls) == (1, 1)


async def test_disconnect_before_body_is_logged_and_raises() -> None:
    fake = FakeUpstream(Script(disconnect_before_body=True))
    with pytest.raises(httpx.RemoteProtocolError):
        await post(fake, *CHAT)
    assert fake.calls == 1


@pytest.mark.parametrize("target", [CHAT, MESSAGES, RESPONSES])
async def test_mid_stream_cut_delivers_the_prefix_then_fails(
    target: Target,
) -> None:
    fake = FakeUpstream(Script(cut_after_frames=2))
    received: list[bytes] = []
    async with fake.client() as client:
        with pytest.raises(httpx.RemoteProtocolError):
            async with client.stream("POST", target[0], json={**target[1], "stream": True}) as r:
                assert r.status_code == 200 and "x-fake-fault" not in r.headers
                async for chunk in r.aiter_bytes():
                    received.append(chunk)
    assert received and b"[DONE]" not in b"".join(received)
    assert b"message_stop" not in b"".join(received)


async def test_hang_then_client_cancel_after_send_is_observable() -> None:
    fake = FakeUpstream(Script(hang=True))
    async with fake.client() as client:
        task = asyncio.ensure_future(client.post(CHAT[0], json={}))
        await fake.wait_for_calls(1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert fake.calls == 1 and fake.cancelled == 1


# --- parser-limit and hostile-usage scripts ---------------------------------------------------


async def test_oversize_event_exceeds_a_limit_the_test_chooses() -> None:
    fake = FakeUpstream(Script(oversize_event_bytes=200_000))
    events = sse_events((await post(fake, *CHAT, stream=True)).text)
    assert max(len(data) for _, data in events) > 200_000


async def test_extra_events_exceed_an_event_count_limit() -> None:
    fake = FakeUpstream(Script(extra_events=5000))
    events = sse_events((await post(fake, *CHAT, stream=True)).text)
    assert len(events) > 5000


async def test_oversize_applies_to_eventstream_and_named_sse_shapes() -> None:
    fake = FakeUpstream(Script(oversize_event_bytes=70_000))
    frames = decode_eventstream((await post(fake, "/model/x/converse-stream", {})).content)
    assert any(len(json.dumps(p)) > 70_000 for _, p in frames)
    named = sse_events((await post(fake, *MESSAGES, stream=True)).text)
    assert max(len(data) for _, data in named) > 70_000


async def test_absurd_usage_is_reported_verbatim() -> None:
    fake = FakeUpstream(Script(usage=ABSURD_USAGE))
    chat = (await post(fake, *CHAT)).json()["usage"]
    assert chat["prompt_tokens"] == 10**12 and chat["completion_tokens"] == 10**12
    stream = sse_events((await post(fake, *MESSAGES, stream=True)).text)
    assert json.loads(stream[-2][1])["usage"]["output_tokens"] == 10**12
