"""OpenAI wire shapes, also used for Azure OpenAI (same bodies, different paths).

Faithful for the fields spendgate reads: usage (with cached and reasoning details), chunk and
named-event framing, error bodies. Message content is a stand-in.
"""

from tests.fakes.frames import sse, sse_json
from tests.fakes.script import FakeUsage
from tests.fakes.shape_types import JSON, SSE, Ctx, Reply, dumps


def _usage(ctx: Ctx) -> FakeUsage:
    usage = ctx.script.usage
    assert isinstance(usage, FakeUsage)
    return usage


def openai_error(status: int, message: str) -> Reply:
    kind = "rate_limit_error" if status == 429 else "invalid_request_error"
    if status >= 500:
        kind = "server_error"
    return Reply(JSON, dumps({"error": {"message": message, "type": kind, "code": None}}))


class _OpenAIBase:
    suffix = ""
    name = ""

    def matches(self, path: str) -> bool:
        return path.endswith(self.suffix)

    def streaming(self, ctx: Ctx) -> bool:
        return ctx.body.get("stream") is True

    def error(self, status: int, message: str) -> Reply:
        return openai_error(status, message)


class Chat(_OpenAIBase):
    name = "openai_chat"
    suffix = "/chat/completions"

    def reply(self, ctx: Ctx) -> Reply:
        u = _usage(ctx)
        body: dict[str, object] = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": str(ctx.body.get("model", "fake")),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello"}}],
        }
        if ctx.script.final_usage:
            body["usage"] = _chat_usage(u)
        return Reply(JSON, dumps(body))

    def stream(self, ctx: Ctx) -> tuple[str, list[bytes]]:
        frames = [
            sse_json(_chunk({"content": text}, finish=None)) for text in ctx.script.stream_deltas()
        ]
        frames.append(sse_json(_chunk({}, finish="stop")))
        options = ctx.body.get("stream_options")
        # Like the real API: the usage-only chunk (empty choices) exists only when asked for.
        if ctx.script.final_usage and isinstance(options, dict) and options.get("include_usage"):
            frames.append(sse_json({"id": "c", "choices": [], "usage": _chat_usage(_usage(ctx))}))
        return SSE, [*frames, sse("[DONE]")]


def _chunk(delta: dict[str, object], finish: str | None) -> dict[str, object]:
    choice = {"index": 0, "delta": delta, "finish_reason": finish}
    return {"id": "c", "object": "chat.completion.chunk", "choices": [choice]}


def _chat_usage(u: FakeUsage) -> dict[str, object]:
    return {
        "prompt_tokens": u.input_tokens,
        "completion_tokens": u.output_tokens,
        "total_tokens": u.input_tokens + u.output_tokens,
        "prompt_tokens_details": {"cached_tokens": u.cached_input_tokens},
        "completion_tokens_details": {"reasoning_tokens": u.reasoning_tokens},
    }


class Responses(_OpenAIBase):
    name = "openai_responses"
    suffix = "/responses"

    def reply(self, ctx: Ctx) -> Reply:
        body: dict[str, object] = {"id": "resp_fake", "status": "completed", "output_text": "Hello"}
        if ctx.script.final_usage:
            body["usage"] = _responses_usage(_usage(ctx))
        return Reply(JSON, dumps(body))

    def stream(self, ctx: Ctx) -> tuple[str, list[bytes]]:
        frames = [sse_json({"type": "response.created"}, "response.created")]
        for text in ctx.script.stream_deltas():
            frames.append(
                sse_json(
                    {"type": "response.output_text.delta", "delta": text},
                    "response.output_text.delta",
                )
            )
        response: dict[str, object] = {"id": "resp_fake", "status": "completed"}
        if ctx.script.final_usage:
            response["usage"] = _responses_usage(_usage(ctx))
        frames.append(
            sse_json({"type": "response.completed", "response": response}, "response.completed")
        )
        return SSE, frames


def _responses_usage(u: FakeUsage) -> dict[str, object]:
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "total_tokens": u.input_tokens + u.output_tokens,
        "input_tokens_details": {"cached_tokens": u.cached_input_tokens},
        "output_tokens_details": {"reasoning_tokens": u.reasoning_tokens},
    }


class Embeddings(_OpenAIBase):
    name = "openai_embeddings"
    suffix = "/embeddings"

    def streaming(self, ctx: Ctx) -> bool:
        return False

    def reply(self, ctx: Ctx) -> Reply:
        u = _usage(ctx)
        body: dict[str, object] = {"object": "list", "data": [{"embedding": [0.0, 0.1]}]}
        if ctx.script.final_usage:
            body["usage"] = {"prompt_tokens": u.input_tokens, "total_tokens": u.input_tokens}
        return Reply(JSON, dumps(body))

    def stream(self, ctx: Ctx) -> tuple[str, list[bytes]]:
        raise NotImplementedError("embeddings do not stream")


class Transcriptions(Embeddings):
    name = "openai_transcriptions"
    suffix = "/audio/transcriptions"

    def reply(self, ctx: Ctx) -> Reply:
        u = _usage(ctx)
        body: dict[str, object] = {"text": "hello"}
        if ctx.script.final_usage:
            body["usage"] = {"type": "duration", "seconds": float(u.audio_seconds)}
        return Reply(JSON, dumps(body))


class Speech(Embeddings):
    name = "openai_speech"
    suffix = "/audio/speech"

    def reply(self, ctx: Ctx) -> Reply:
        # Raw audio bytes, no usage: speech is billed by characters measured by the gateway.
        return Reply("audio/mpeg", b"ID3\x00fake-audio")
