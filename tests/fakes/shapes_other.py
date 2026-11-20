"""Bedrock Converse, Gemini, Azure Speech fast transcription and ElevenLabs shapes.

Fidelity: Bedrock ConverseStream uses the real AWS event-stream binary framing (CRCs verified by
the self-tests), only without request signing. Gemini streaming is real SSE with `alt=sse` and the
documented JSON-array stream without it. Azure Speech and ElevenLabs reproduce only the fields
spendgate reads (duration; audio bytes plus billed characters); their paths, fields and headers
were checked against the provider docs on 2026-11-17.
"""

from tests.fakes.frames import eventstream_frame, sse_json
from tests.fakes.script import FakeUsage
from tests.fakes.shape_types import JSON, SSE, Ctx, Reply, dumps

EVENTSTREAM = "application/vnd.amazon.eventstream"


def _usage(ctx: Ctx) -> FakeUsage:
    usage = ctx.script.usage
    assert isinstance(usage, FakeUsage)
    return usage


class _NoStream:
    def streaming(self, ctx: Ctx) -> bool:
        return False

    def stream(self, ctx: Ctx) -> tuple[str, list[bytes]]:
        raise NotImplementedError("this shape does not stream")


class BedrockConverse:
    name = "bedrock_converse"

    def matches(self, path: str) -> bool:
        return path.endswith(("/converse", "/converse-stream"))

    def streaming(self, ctx: Ctx) -> bool:
        return ctx.path.endswith("/converse-stream")

    def reply(self, ctx: Ctx) -> Reply:
        body: dict[str, object] = {
            "output": {"message": {"role": "assistant", "content": [{"text": "Hello"}]}},
            "stopReason": "end_turn",
            "metrics": {"latencyMs": 1},
        }
        if ctx.script.final_usage:
            body["usage"] = _converse_usage(_usage(ctx))
        return Reply(JSON, dumps(body))

    def stream(self, ctx: Ctx) -> tuple[str, list[bytes]]:
        frames = [eventstream_frame("messageStart", {"role": "assistant"})]
        for text in ctx.script.stream_deltas():
            frames.append(
                eventstream_frame(
                    "contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": text}}
                )
            )
        frames.append(eventstream_frame("contentBlockStop", {"contentBlockIndex": 0}))
        frames.append(eventstream_frame("messageStop", {"stopReason": "end_turn"}))
        if ctx.script.final_usage:  # usage rides the last `metadata` event
            meta = {"usage": _converse_usage(_usage(ctx)), "metrics": {"latencyMs": 1}}
            frames.append(eventstream_frame("metadata", meta))
        return EVENTSTREAM, frames

    def error(self, status: int, message: str) -> Reply:
        return Reply(JSON, dumps({"message": message}))


def _converse_usage(u: FakeUsage) -> dict[str, object]:
    return {
        "inputTokens": u.input_tokens,
        "outputTokens": u.output_tokens,
        "totalTokens": u.input_tokens + u.output_tokens,
        "cacheReadInputTokens": u.cached_input_tokens,
        "cacheWriteInputTokens": u.cache_write_tokens,
    }


class Gemini:
    name = "gemini"

    def matches(self, path: str) -> bool:
        return path.endswith((":generateContent", ":streamGenerateContent"))

    def streaming(self, ctx: Ctx) -> bool:
        return ctx.path.endswith(":streamGenerateContent")

    def _chunk(self, ctx: Ctx, text: str) -> dict[str, object]:
        chunk: dict[str, object] = {
            "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}]
        }
        if ctx.script.final_usage:
            u = _usage(ctx)
            chunk["usageMetadata"] = {
                "promptTokenCount": u.input_tokens,
                "candidatesTokenCount": u.output_tokens,
                "totalTokenCount": u.input_tokens + u.output_tokens + u.reasoning_tokens,
                "cachedContentTokenCount": u.cached_input_tokens,
                "thoughtsTokenCount": u.reasoning_tokens,
            }
        return chunk

    def reply(self, ctx: Ctx) -> Reply:
        return Reply(JSON, dumps(self._chunk(ctx, "Hello")))

    def stream(self, ctx: Ctx) -> tuple[str, list[bytes]]:
        chunks = [self._chunk(ctx, text) for text in ctx.script.stream_deltas()]
        if ctx.query.get("alt") == "sse":
            return SSE, [sse_json(c) for c in chunks]
        # Without alt=sse the API streams one JSON array, element by element.
        parts = [(b"[" if i == 0 else b",\r\n") + dumps(c) for i, c in enumerate(chunks)]
        return JSON, [*parts, b"]"]

    def error(self, status: int, message: str) -> Reply:
        names = {429: "RESOURCE_EXHAUSTED", 500: "INTERNAL", 503: "UNAVAILABLE"}
        err = {"code": status, "message": message, "status": names.get(status, "INVALID_ARGUMENT")}
        return Reply(JSON, dumps({"error": err}))


class AzureSpeech(_NoStream):
    name = "azure_speech"

    def matches(self, path: str) -> bool:
        return path.endswith("/transcriptions:transcribe")

    def reply(self, ctx: Ctx) -> Reply:
        millis = int(_usage(ctx).audio_seconds * 1000)
        # Fields per Microsoft Learn "Fast transcription" (api-version 2025-10-15).
        phrase = {
            "channel": 0, "offsetMilliseconds": 0, "durationMilliseconds": millis,
            "text": "hello", "words": [], "locale": "en-US", "confidence": 0.9,
        }  # fmt: skip
        body = {
            "durationMilliseconds": millis,
            "combinedPhrases": [{"channel": 0, "text": "hello"}],
            "phrases": [phrase],
        }
        return Reply(JSON, dumps(body))

    def error(self, status: int, message: str) -> Reply:
        return Reply(JSON, dumps({"code": str(status), "message": message}))


class ElevenLabs(_NoStream):
    name = "elevenlabs"

    def matches(self, path: str) -> bool:
        return "/v1/text-to-speech/" in path

    def reply(self, ctx: Ctx) -> Reply:
        cost = str(_usage(ctx).characters)
        return Reply("audio/mpeg", b"ID3\x00fake-audio", (("character-cost", cost),))

    def error(self, status: int, message: str) -> Reply:
        return Reply(JSON, dumps({"detail": {"status": "error", "message": message}}))
