"""Shared builders for the Bedrock contract tests."""

from collections.abc import Mapping
from dataclasses import replace

from spendgate.providers.bedrock import AwsCredentials, Bedrock
from spendgate.providers.port import AuthMode, CleanRequest, Deployment, Json, SseEvent
from spendgate.providers.sse import SseParser
from tests.fakes import BASE_URL

# AWS's documented example credentials (docs.aws.amazon.com), not real.
EXAMPLE = AwsCredentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
PROFILE = "eu.anthropic.claude-haiku-5-5-v1:0"
CHAT = "/v1/chat/completions"


def deployment(model: str = PROFILE) -> Deployment:
    return Deployment("bedrock", BASE_URL, model, AuthMode.SIGV4, region="eu-west-1")


def adapter() -> Bedrock:
    return Bedrock(lambda: EXAMPLE)


def request(body: dict[str, object], *, stream: bool = False) -> CleanRequest:
    return CleanRequest(CHAT, "gateway-name", body, stream=stream)  # type: ignore[arg-type]


def dig(value: Json, *path: str | int) -> Json:
    """Walk parsed JSON by keys and indexes; a missing step gives None."""
    for key in path:
        if isinstance(key, int) and isinstance(value, list) and -len(value) <= key < len(value):
            value = value[key]
        elif isinstance(key, str) and isinstance(value, Mapping):
            value = value.get(key)
        else:
            return None
    return value


def wire(raw: bytes) -> list[SseEvent]:
    """The SSE events a client would parse out of the relay's output bytes."""
    parser = SseParser()
    return [*parser.feed(raw), *parser.close()]


def converting(body: dict[str, object], *, stream: bool = False) -> CleanRequest:
    """A request carrying the id, time and model the pipeline would set for the answer."""
    return replace(
        request(body, stream=stream), model="gw-model", response_id="chatcmpl-1", created=7
    )
