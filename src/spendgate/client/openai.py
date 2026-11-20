"""OpenAI SDK helper for proxy mode. The SDK is imported lazily (optional extra `openai`)."""

from typing import TYPE_CHECKING, Literal, cast, overload

from spendgate.client.session import attach_session_hook, attribution_headers

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    import openai as _sdk


@overload
def client(
    base_url: str,
    key: str,
    *,
    session: str | None = ...,
    user: str | None = ...,
    tags: "Iterable[str]" = ...,
    async_: Literal[False] = ...,
    **sdk_kwargs: object,
) -> "_sdk.OpenAI": ...
@overload
def client(
    base_url: str,
    key: str,
    *,
    session: str | None = ...,
    user: str | None = ...,
    tags: "Iterable[str]" = ...,
    async_: Literal[True],
    **sdk_kwargs: object,
) -> "_sdk.AsyncOpenAI": ...
def client(  # noqa: PLR0913
    base_url: str,
    key: str,
    *,
    session: str | None = None,
    user: str | None = None,
    tags: "Iterable[str]" = (),
    async_: bool = False,
    **sdk_kwargs: object,
) -> "_sdk.OpenAI | _sdk.AsyncOpenAI":
    """An `openai.OpenAI` (or `AsyncOpenAI`) routed through the proxy with attribution headers.

    `session` is a static default; `spendgate.client.session(...)` overrides it per call.
    Other keyword arguments go to the SDK unchanged.
    """
    import openai  # noqa: PLC0415 - optional extra, imported only when used

    static = attribution_headers(session, user, tags)
    extra = sdk_kwargs.pop("default_headers", None)
    headers = {**(extra if isinstance(extra, dict) else {}), **static}
    http = sdk_kwargs.pop("http_client", None)
    if http is None:
        http = openai.DefaultAsyncHttpxClient() if async_ else openai.DefaultHttpxClient()
    attach_session_hook(http)
    cls = openai.AsyncOpenAI if async_ else openai.OpenAI
    # sdk_kwargs are the SDK's own keyword arguments; the SDK validates them.
    build = cast("Callable[..., _sdk.OpenAI | _sdk.AsyncOpenAI]", cls)
    return build(
        base_url=base_url, api_key=key, default_headers=headers, http_client=http, **sdk_kwargs
    )
