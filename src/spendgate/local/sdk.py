"""SDK clients wired to a local transport. SDKs are optional extras, imported only when used.

The OpenAI SDK uses `httpx`, the Anthropic SDK 2.x uses `httpx2` (see `bridge.py`), hence two
transport families. The base URL is a placeholder host that is never resolved: the transport
answers every request itself. The API key is a placeholder too; the provider credential stays
in the config and is added by the pipeline's provider adapter.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import anthropic as _anthropic
    import openai as _openai

    from spendgate.local.transport_httpx2 import AsyncLocalTransport2, LocalTransport2

_HOST = "http://spendgate.local"
_KEY = "spendgate-local"


def openai_sync(transport: "LocalTransport2", sdk: dict[str, object]) -> "_openai.OpenAI":
    import openai  # noqa: PLC0415 - optional extra

    build = cast("Callable[..., _openai.OpenAI]", openai.OpenAI)
    http = openai.DefaultHttpxClient(transport=transport)
    return build(base_url=f"{_HOST}/v1", api_key=_KEY, http_client=http, **sdk)


def openai_async(
    transport: "AsyncLocalTransport2", sdk: dict[str, object]
) -> "_openai.AsyncOpenAI":
    import openai  # noqa: PLC0415 - optional extra

    build = cast("Callable[..., _openai.AsyncOpenAI]", openai.AsyncOpenAI)
    http = openai.DefaultAsyncHttpxClient(transport=transport)
    return build(base_url=f"{_HOST}/v1", api_key=_KEY, http_client=http, **sdk)


def anthropic_sync(transport: "LocalTransport2", sdk: dict[str, object]) -> "_anthropic.Anthropic":
    import anthropic  # noqa: PLC0415 - optional extra

    build = cast("Callable[..., _anthropic.Anthropic]", anthropic.Anthropic)
    http = anthropic.DefaultHttpxClient(transport=transport)
    return build(base_url=_HOST, api_key=_KEY, http_client=http, **sdk)


def anthropic_async(
    transport: "AsyncLocalTransport2", sdk: dict[str, object]
) -> "_anthropic.AsyncAnthropic":
    import anthropic  # noqa: PLC0415 - optional extra

    build = cast("Callable[..., _anthropic.AsyncAnthropic]", anthropic.AsyncAnthropic)
    http = anthropic.DefaultAsyncHttpxClient(transport=transport)
    return build(base_url=_HOST, api_key=_KEY, http_client=http, **sdk)
