"""OpenAI chat -> Converse translation table and refusals (protocol 2.7)."""

import json

import pytest

from spendgate.core.errors import ErrorCode
from spendgate.providers.bedrock import Bedrock
from spendgate.providers.bedrock_request import converse_body
from spendgate.providers.port import AuthMode, CleanRequest, Deployment, Json, Untranslatable
from tests.contract.providers.bedrock.helpers import PROFILE, adapter, deployment, request
from tests.contract.providers.ceiling import ceiling_of

PNG = "iVBORw0KGgo="  # base64 of a PNG signature, enough for the format check
WEATHER: Json = {
    "type": "function",
    "function": {"name": "weather", "description": "d", "parameters": {"type": "object"}},
}
USER: Json = {"role": "user", "content": "hi"}

TABLE: list[tuple[str, dict[str, Json], dict[str, Json]]] = [
    (
        "system and developer go to system, user to messages",
        {"messages": [
            {"role": "system", "content": "s"}, {"role": "developer", "content": "d"}, USER,
        ]},
        {
            "system": [{"text": "s"}, {"text": "d"}],
            "messages": [{"role": "user", "content": [{"text": "hi"}]}],
        },
    ),
    (
        "sampling fields",
        {"messages": [USER], "max_tokens": 50, "temperature": 0.2, "top_p": 0.9, "stop": "END"},
        {
            "messages": [{"role": "user", "content": [{"text": "hi"}]}],
            "inferenceConfig": {
                "maxTokens": 50, "temperature": 0.2, "topP": 0.9, "stopSequences": ["END"],
            },
        },
    ),
    (
        "both token limits: only the held (smaller) one is sent",
        {"messages": [USER], "max_tokens": 50, "max_completion_tokens": 20},
        {
            "messages": [{"role": "user", "content": [{"text": "hi"}]}],
            "inferenceConfig": {"maxTokens": 20},
        },
    ),
    (
        "data URL image",
        {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "see"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{PNG}"}},
        ]}]},
        {"messages": [{"role": "user", "content": [
            {"text": "see"}, {"image": {"format": "jpeg", "source": {"bytes": PNG}}},
        ]}]},
    ),
    (
        "tool call and result, same-role turns merged",
        {"messages": [
            USER,
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "weather", "arguments": '{"q":"x"}'}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "sunny"},
            {"role": "user", "content": "thanks"},
        ], "tools": [WEATHER]},
        {
            "messages": [
                {"role": "user", "content": [{"text": "hi"}]},
                {"role": "assistant", "content": [
                    {"toolUse": {"toolUseId": "c1", "name": "weather", "input": {"q": "x"}}},
                ]},
                {"role": "user", "content": [
                    {"toolResult": {"toolUseId": "c1", "content": [{"text": "sunny"}]}},
                    {"text": "thanks"},
                ]},
            ],
            "toolConfig": {"tools": [{"toolSpec": {
                "name": "weather", "description": "d", "inputSchema": {"json": {"type": "object"}},
            }}]},
        },
    ),
    (
        "tool_choice required and named",
        {"messages": [USER], "tools": [WEATHER], "tool_choice": "required"},
        {
            "messages": [{"role": "user", "content": [{"text": "hi"}]}],
            "toolConfig": {"tools": [{"toolSpec": {
                "name": "weather", "description": "d", "inputSchema": {"json": {"type": "object"}},
            }}], "toolChoice": {"any": {}}},
        },
    ),
    (
        "tool_choice none sends no tools",
        {"messages": [USER], "tools": [WEATHER], "tool_choice": "none"},
        {"messages": [{"role": "user", "content": [{"text": "hi"}]}]},
    ),
    (
        "json_schema response format",
        {"messages": [USER], "response_format": {"type": "json_schema", "json_schema": {
            "name": "n", "schema": {"type": "object"},
        }}},
        {
            "messages": [{"role": "user", "content": [{"text": "hi"}]}],
            "outputConfig": {"textFormat": {"type": "json_schema", "structure": {"jsonSchema": {
                "schema": '{"type": "object"}', "name": "n",
            }}}},
        },
    ),
    (
        "consumed by the gateway, never forwarded: user, stream, stream_options, store=false",
        {"messages": [USER], "user": "u1", "stream": True, "store": False,
         "stream_options": {"include_usage": True}},
        {"messages": [{"role": "user", "content": [{"text": "hi"}]}]},
    ),
]  # fmt: skip


@pytest.mark.parametrize(("case", "chat", "converse"), TABLE, ids=[t[0] for t in TABLE])
def test_chat_translates_to_converse_as_listed(
    case: str, chat: dict[str, Json], converse: dict[str, Json]
) -> None:
    # Every Converse body carries the held ceiling as `maxTokens`, listed or not in the row.
    config = converse.get("inferenceConfig")
    listed = dict(config) if isinstance(config, dict) else {}
    expected = {**converse, "inferenceConfig": {"maxTokens": ceiling_of(chat), **listed}}
    assert converse_body({"model": "alias", **chat}, ceiling_of(chat)) == expected


REFUSED: list[tuple[str, dict[str, Json]]] = [
    ("seed", {"seed": 1}),
    ("n", {"n": 1}),
    ("presence_penalty", {"presence_penalty": 0.1}),
    ("parallel_tool_calls", {"parallel_tool_calls": True}),
    ("reasoning_effort", {"reasoning_effort": "low"}),
    ("logit_bias", {"logit_bias": {}}),
    ("store", {"store": True}),
    ("tool_choice", {"tool_choice": "auto"}),  # without tools
    ("response_format", {"response_format": {"type": "json_object"}}),
    ("tools[0].function.strict", {"tools": [
        {"type": "function", "function": {"name": "w", "strict": True}},
    ]}),
    ("tools[0]", {"tools": [{"type": "web_search"}]}),
    ("messages[0].content[0].image_url", {"messages": [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}]},
    ]}),
    ("messages[0].content[0].type", {"messages": [
        {"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]},
    ]}),
    ("messages[0].role", {"messages": [{"role": "function", "content": "x"}]}),
    ("messages[1].tool_calls[0].function.arguments", {"messages": [USER, {
        "role": "assistant", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "w", "arguments": "not json"}},
        ]}]}),
    ("temperature", {"temperature": "hot"}),
    ("stop", {"stop": [1]}),
]  # fmt: skip


@pytest.mark.parametrize(("param", "extra"), REFUSED, ids=[r[0] for r in REFUSED])
def test_everything_else_is_refused_with_invalid_request_naming_the_param(
    param: str, extra: dict[str, Json]
) -> None:
    body: dict[str, Json] = {"messages": [USER], **extra}
    with pytest.raises(Untranslatable) as err:
        converse_body(body, ceiling_of(body))
    assert err.value.refusal.code is ErrorCode.INVALID_REQUEST
    assert err.value.refusal.param == param


def test_translate_takes_model_region_and_path_from_the_deployment_not_the_client() -> None:
    up = adapter().translate(request({"model": "client-picked", "messages": [USER]}), deployment())
    assert up.url == f"https://upstream.test/model/{PROFILE.replace(':', '%3A')}/converse"
    assert "client-picked" not in up.body.decode()
    assert "model" not in json.loads(up.body)
    assert "eu-west-1/bedrock/aws4_request" in up.headers["Authorization"]
    streamed = adapter().translate(request({"messages": [USER]}, stream=True), deployment())
    assert streamed.url.endswith("/converse-stream") and streamed.stream


def test_translate_needs_a_sigv4_deployment_with_a_region() -> None:
    plain = Deployment("bedrock", "https://x", "m", AuthMode.API_KEY, region="eu-west-1")
    with pytest.raises(ValueError, match="sigv4"):
        Bedrock().translate(request({"messages": [USER]}), plain)


def test_validate_refuses_other_endpoints_and_translate_raises_untranslatable() -> None:
    other = CleanRequest("/v1/embeddings", "m", {"input": "x"})
    refusal = adapter().validate(other)
    assert refusal is not None and refusal.param == "endpoint"
    assert adapter().validate(request({"messages": [USER]})) is None
    with pytest.raises(Untranslatable) as err:
        adapter().translate(other, deployment())
    assert err.value.refusal == refusal


def _image_chat(url: str) -> dict[str, Json]:
    part: Json = {"type": "image_url", "image_url": {"url": url}}
    return {"messages": [{"role": "user", "content": [part]}], "max_tokens": 5}


@pytest.mark.parametrize("fmt", ["png", "jpeg", "webp", "gif"])
def test_m14_the_four_image_types_map_to_converse_formats(fmt: str) -> None:
    body = converse_body(_image_chat(f"data:image/{fmt};base64,{PNG}"), 5)
    image: Json = {"image": {"format": fmt, "source": {"bytes": PNG}}}
    assert body["messages"] == [{"role": "user", "content": [image]}]


@pytest.mark.parametrize("mime", ["image/jpg", "image/PNG", "video/mp4", "application/pdf"])
def test_m14_other_inline_media_is_refused(mime: str) -> None:
    with pytest.raises(Untranslatable) as refused:
        converse_body(_image_chat(f"data:{mime};base64,{PNG}"), 5)
    assert refused.value.refusal.param == "messages[0].content[0].image_url"
