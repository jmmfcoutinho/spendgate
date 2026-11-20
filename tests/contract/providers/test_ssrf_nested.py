"""T-SSRF-01: no nested content position lets a client make the provider fetch a remote URL.

Every position that can carry a content part is exercised for OpenAI, Azure OpenAI (same shapes)
and Anthropic. `remote_ok` (the fail-open option) is the only thing that admits a remote image.
"""

import pytest

from spendgate.providers.anthropic import AnthropicProvider
from spendgate.providers.azure_openai import AzureOpenAIProvider
from spendgate.providers.openai import OpenAIProvider
from spendgate.providers.port import CleanRequest, Json

REMOTE = "https://evil.test/x.png"
INLINE = "data:image/png;base64,AAAA"


def _responses(output: Json) -> dict[str, Json]:
    item: Json = {"type": "function_call_output", "call_id": "c1", "output": output}
    return {"model": "m", "input": [item]}


def _responses_content(part: Json) -> dict[str, Json]:
    return {"model": "m", "input": [{"role": "user", "content": [part]}]}


def _chat(part: Json, role: str = "user") -> dict[str, Json]:
    return {"model": "m", "messages": [{"role": role, "content": [part]}]}


def _image(url: str) -> Json:
    return {"type": "input_image", "image_url": url}


OPENAI_BAD: list[tuple[str, dict[str, Json]]] = [
    ("/v1/responses", _responses([_image(REMOTE)])),
    ("/v1/responses", _responses([{"type": "input_file", "file_url": REMOTE}])),
    ("/v1/responses", _responses([{"type": "input_file", "file_id": "file-1"}])),
    ("/v1/responses", _responses({"type": "input_image", "image_url": REMOTE})),
    ("/v1/responses", _responses([{"type": "input_text", "text": "x"}, _image(REMOTE)])),
    ("/v1/responses", _responses_content(_image(REMOTE))),
    ("/v1/responses", _responses_content({"type": "input_file", "file_url": REMOTE})),
    ("/v1/chat/completions", _chat({"type": "image_url", "image_url": {"url": REMOTE}})),
    ("/v1/chat/completions", _chat({"type": "image_url", "image_url": {"url": REMOTE}}, "tool")),
    ("/v1/chat/completions", _chat({"type": "file", "file": {"file_id": "f"}})),
]
OPENAI_OK: list[tuple[str, dict[str, Json]]] = [
    ("/v1/responses", _responses("plain string output")),
    ("/v1/responses", _responses([_image(INLINE)])),
    ("/v1/responses", _responses([{"type": "input_text", "text": "x"}])),
    ("/v1/chat/completions", _chat({"type": "image_url", "image_url": {"url": INLINE}})),
]


@pytest.mark.parametrize("cls", [OpenAIProvider, AzureOpenAIProvider])
@pytest.mark.parametrize(("endpoint", "body"), OPENAI_BAD)
def test_t_ssrf_01_openai_nested_remote_content_is_refused(
    cls: type[OpenAIProvider] | type[AzureOpenAIProvider], endpoint: str, body: dict[str, Json]
) -> None:
    assert cls().validate(CleanRequest(endpoint, "m", body)) is not None


@pytest.mark.parametrize("cls", [OpenAIProvider, AzureOpenAIProvider])
@pytest.mark.parametrize(("endpoint", "body"), OPENAI_OK)
def test_t_ssrf_01_openai_inline_content_is_accepted(
    cls: type[OpenAIProvider] | type[AzureOpenAIProvider], endpoint: str, body: dict[str, Json]
) -> None:
    assert cls().validate(CleanRequest(endpoint, "m", body)) is None


@pytest.mark.parametrize("cls", [OpenAIProvider, AzureOpenAIProvider])
def test_t_ssrf_01_remote_ok_admits_remote_images_in_function_output_but_never_files(
    cls: type[OpenAIProvider] | type[AzureOpenAIProvider],
) -> None:
    provider = cls(remote_ok=True)
    ok = CleanRequest("/v1/responses", "m", _responses([_image(REMOTE)]))
    assert provider.validate(ok) is None
    file = CleanRequest(
        "/v1/responses", "m", _responses([{"type": "input_file", "file_url": REMOTE}])
    )
    assert provider.validate(file) is not None


def _anthropic(content: Json, *, system: Json = None) -> dict[str, Json]:
    body: dict[str, Json] = {
        "model": "m",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": content}],
    }
    if system is not None:
        body["system"] = system
    return body


URL_SOURCE: Json = {"type": "url", "url": REMOTE}
ANTHROPIC_BAD: list[dict[str, Json]] = [
    _anthropic([{"type": "image", "source": URL_SOURCE}]),
    _anthropic([{"type": "image", "source": {"type": "file", "file_id": "f"}}]),
    _anthropic(
        [
            {
                "type": "tool_result",
                "tool_use_id": "t",
                "content": [{"type": "image", "source": URL_SOURCE}],
            }
        ]
    ),
    _anthropic([{"type": "document", "source": URL_SOURCE}]),
    _anthropic([{"type": "tool_result", "tool_use_id": "t", "content": [{"type": "document"}]}]),
    _anthropic("hi", system=[{"type": "image", "source": URL_SOURCE}]),
    _anthropic([{"type": "tool_result", "tool_use_id": "t", "content": [{"type": "tool_result"}]}]),
]


@pytest.mark.parametrize("body", ANTHROPIC_BAD)
def test_t_ssrf_01_anthropic_nested_remote_content_is_refused(body: dict[str, Json]) -> None:
    assert AnthropicProvider().validate(CleanRequest("/v1/messages", "m", body)) is not None


def test_t_ssrf_01_anthropic_inline_nested_image_is_accepted() -> None:
    source: Json = {"type": "base64", "media_type": "image/png", "data": "AAAA"}
    body = _anthropic(
        [
            {
                "type": "tool_result",
                "tool_use_id": "t",
                "content": [{"type": "image", "source": source}],
            }
        ]
    )
    assert AnthropicProvider().validate(CleanRequest("/v1/messages", "m", body)) is None
