"""M-15 per adapter: the upstream body carries one ceiling, `CleanRequest.output_ceiling`, alone.

The pipeline holds that value. Whatever ceiling fields the client body still names are dropped, and
a ceiling endpoint without a held ceiling is refused before any contact (fail closed).
"""

import json
from collections.abc import Callable, Mapping

import pytest

from spendgate.providers.anthropic import AnthropicProvider
from spendgate.providers.azure_openai import AzureOpenAIProvider
from spendgate.providers.bedrock import Bedrock
from spendgate.providers.google import GoogleProvider
from spendgate.providers.openai import OpenAIProvider
from spendgate.providers.port import (
    AuthMode,
    CleanRequest,
    Deployment,
    Json,
    Provider,
    Untranslatable,
)
from tests.contract.providers.bedrock.helpers import EXAMPLE

CHAT = "/v1/chat/completions"
RESPONSES = "/v1/responses"
MESSAGES = "/v1/messages"
HELD = 7
USER: Json = [{"role": "user", "content": "hi"}]
FAKE_KEY = "fake-key-not-real"  # pragma: allowlist secret
CEILING_SPELLINGS = ("max_tokens", "max_completion_tokens", "max_output_tokens")

type Reader = Callable[[Mapping[str, Json]], Json]


def _top(name: str) -> Reader:
    def read(body: Mapping[str, Json]) -> Json:
        others = [k for k in CEILING_SPELLINGS if k in body and k != name]
        assert others == [], f"a second ceiling field was forwarded: {others}"
        return body[name]

    return read


def _nested(outer: str, name: str) -> Reader:
    def read(body: Mapping[str, Json]) -> Json:
        assert not any(k in body for k in CEILING_SPELLINGS)
        config = body[outer]
        assert isinstance(config, Mapping)
        return config[name]

    return read


def _deployment(provider: str, auth: AuthMode = AuthMode.API_KEY) -> Deployment:
    credential = "" if auth is AuthMode.SIGV4 else FAKE_KEY
    region = "eu-west-1" if auth is AuthMode.SIGV4 else None
    return Deployment(
        provider, "https://upstream.test", "m-up", auth, region, credential=credential
    )


# (case id, adapter, deployment, endpoint, client body with every ceiling it may send, reader)
CASES: list[tuple[str, Callable[[], Provider], Deployment, str, dict[str, Json], Reader]] = [
    ("openai chat", OpenAIProvider, _deployment("openai"), CHAT,
     {"messages": USER, "max_tokens": 900, "max_completion_tokens": 30_000},
     _top("max_completion_tokens")),
    ("openai responses", OpenAIProvider, _deployment("openai"), RESPONSES,
     {"input": "hi", "max_output_tokens": 30_000}, _top("max_output_tokens")),
    ("azure_openai chat", AzureOpenAIProvider, _deployment("azure_openai"), CHAT,
     {"messages": USER, "max_tokens": 900, "max_completion_tokens": 30_000},
     _top("max_completion_tokens")),
    ("azure_openai responses", AzureOpenAIProvider, _deployment("azure_openai"), RESPONSES,
     {"input": "hi", "max_output_tokens": 30_000}, _top("max_output_tokens")),
    ("anthropic messages", AnthropicProvider, _deployment("anthropic"), MESSAGES,
     {"model": "m", "messages": USER, "max_tokens": 30_000}, _top("max_tokens")),
    ("anthropic messages, other spellings", AnthropicProvider, _deployment("anthropic"), MESSAGES,
     {"model": "m", "messages": USER, "max_tokens": 900, "max_completion_tokens": 30_000,
      "max_output_tokens": 30_000}, _top("max_tokens")),
    ("bedrock chat", lambda: Bedrock(lambda: EXAMPLE), _deployment("bedrock", AuthMode.SIGV4),
     CHAT, {"messages": USER, "max_tokens": 900, "max_completion_tokens": 30_000},
     _nested("inferenceConfig", "maxTokens")),
    ("google chat", GoogleProvider, _deployment("google"), CHAT,
     {"messages": USER, "max_tokens": 900, "max_completion_tokens": 30_000},
     _nested("generationConfig", "maxOutputTokens")),
]  # fmt: skip
IDS = [c[0] for c in CASES]


@pytest.mark.parametrize(("case", "make", "dep", "endpoint", "body", "read"), CASES, ids=IDS)
def test_m15_the_forwarded_ceiling_is_the_held_one_alone(  # noqa: PLR0913, PLR0917 - one case row
    case: str, make: Callable[[], Provider], dep: Deployment, endpoint: str,
    body: dict[str, Json], read: Reader,
) -> None:  # fmt: skip
    req = CleanRequest(endpoint, "m", body, output_ceiling=HELD)
    sent = json.loads(make().translate(req, dep).body)
    assert read(sent) == HELD


@pytest.mark.parametrize(("case", "make", "dep", "endpoint", "body", "read"), CASES, ids=IDS)
def test_m15_a_ceiling_endpoint_without_a_held_ceiling_is_refused(  # noqa: PLR0913, PLR0917
    case: str, make: Callable[[], Provider], dep: Deployment, endpoint: str,
    body: dict[str, Json], read: Reader,
) -> None:  # fmt: skip
    with pytest.raises(Untranslatable) as refused:
        make().translate(CleanRequest(endpoint, "m", body), dep)
    assert refused.value.refusal.param in CEILING_SPELLINGS


@pytest.mark.parametrize("bad", [True, 0, -1, None])
@pytest.mark.parametrize("endpoint", [CHAT, RESPONSES])
def test_m15_openai_shapes_refuse_a_held_ceiling_that_is_not_a_positive_int(
    endpoint: str, bad: object
) -> None:
    body: dict[str, Json] = {"messages": USER} if endpoint == CHAT else {"input": "hi"}
    req = CleanRequest(endpoint, "m", body, output_ceiling=bad)  # type: ignore[arg-type]
    for make, dep in ((OpenAIProvider, "openai"), (AzureOpenAIProvider, "azure_openai")):
        with pytest.raises(Untranslatable):
            make().translate(req, _deployment(dep))
