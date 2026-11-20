"""Anthropic Messages shape: native JSON usage and the named SSE events.

Event order follows the documented stream: message_start (input and cache usage), content block
events, message_delta (output usage), message_stop, with pings between.
"""

from tests.fakes.frames import sse_json
from tests.fakes.script import FakeUsage
from tests.fakes.shape_types import JSON, SSE, Ctx, Reply, dumps


def _usage(ctx: Ctx) -> FakeUsage:
    usage = ctx.script.usage
    assert isinstance(usage, FakeUsage)
    return usage


def _input_usage(u: FakeUsage) -> dict[str, object]:
    return {
        "input_tokens": u.input_tokens,
        "cache_creation_input_tokens": u.cache_write_tokens,
        "cache_read_input_tokens": u.cached_input_tokens,
        "output_tokens": 1,
    }


class Messages:
    name = "anthropic_messages"

    def matches(self, path: str) -> bool:
        return path.endswith("/v1/messages")

    def streaming(self, ctx: Ctx) -> bool:
        return ctx.body.get("stream") is True

    def reply(self, ctx: Ctx) -> Reply:
        u = _usage(ctx)
        body: dict[str, object] = {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello"}],
            "stop_reason": "end_turn",
        }
        if ctx.script.final_usage:
            body["usage"] = {**_input_usage(u), "output_tokens": u.output_tokens}
        return Reply(JSON, dumps(body))

    def stream(self, ctx: Ctx) -> tuple[str, list[bytes]]:
        u = _usage(ctx)
        message: dict[str, object] = {"id": "msg_fake", "type": "message", "role": "assistant"}
        if ctx.script.final_usage:
            message["usage"] = _input_usage(u)
        frames = [
            sse_json({"type": "message_start", "message": message}, "message_start"),
            sse_json(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                "content_block_start",
            ),
            sse_json({"type": "ping"}, "ping"),
        ]
        for text in ctx.script.stream_deltas():
            frames.append(
                sse_json(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    },
                    "content_block_delta",
                )
            )
        frames.append(sse_json({"type": "content_block_stop", "index": 0}, "content_block_stop"))
        delta: dict[str, object] = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
        if ctx.script.final_usage:
            delta["usage"] = {"output_tokens": u.output_tokens}
        frames.append(sse_json(delta, "message_delta"))
        frames.append(sse_json({"type": "message_stop"}, "message_stop"))
        return SSE, frames

    def error(self, status: int, message: str) -> Reply:
        kinds = {429: "rate_limit_error", 529: "overloaded_error", 500: "api_error"}
        kind = kinds.get(status, "invalid_request_error" if status < 500 else "api_error")
        return Reply(JSON, dumps({"type": "error", "error": {"type": kind, "message": message}}))
