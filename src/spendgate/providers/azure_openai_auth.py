"""Entra ID bearer tokens for Azure OpenAI: a cached, injectable token source.

`azure-identity` is an optional extra, so it is imported only when no credential object was
injected, and only here. A refresh is a blocking network call to Entra (typically 100 ms to a
few seconds), so the adapter's async `prepare` makes it in a worker thread (`providers.refresh`)
and `translate` then reads the cache. The cache makes refreshes rare: one per token lifetime.
"""

import threading
import time
from collections.abc import Callable
from typing import Protocol

# Refresh this long before expiry so a request never leaves with a token about to lapse.
REFRESH_MARGIN_SECONDS = 300
# The scope Microsoft documents for Azure OpenAI; `ai.azure.com` is its newer alias for the v1 path.
CLASSIC_SCOPE = "https://cognitiveservices.azure.com/.default"
V1_SCOPE = "https://ai.azure.com/.default"


class AccessToken(Protocol):
    """The shape of `azure.core.credentials.AccessToken`; tests pass a plain fake."""

    @property
    def token(self) -> str: ...

    @property
    def expires_on(self) -> int: ...  # Unix seconds


class TokenCredential(Protocol):
    def get_token(self, *scopes: str) -> AccessToken: ...


def default_credential() -> TokenCredential:
    """`DefaultAzureCredential` (env, workload identity, managed identity, developer login)."""
    try:
        from azure.identity import DefaultAzureCredential  # noqa: PLC0415 - optional extra
    except ImportError as missing:  # pragma: no cover - the extra is installed in CI
        raise RuntimeError(
            "Entra ID auth needs the optional extra: pip install 'spendgate[azure]'"
        ) from missing
    return DefaultAzureCredential()


class EntraTokens:
    """Bearer tokens per scope, cached until shortly before they expire.

    No lock is ever held across `get_token`: a slow fetch for one scope must not make a reader of
    another scope wait (a reader on the event loop would stall it). The cache is a dict of
    immutable tuples, so reads and writes are atomic; one fetch at a time per scope is the job
    of the caller (`Refreshes` in the adapter's `prepare`).
    """

    def __init__(
        self,
        credential: TokenCredential | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._credential = credential
        self._clock = clock
        self._create = threading.Lock()  # guards only the lazy default credential
        self._cache: dict[str, tuple[str, int]] = {}  # scope -> (token, expires_on)

    def cached(self, scope: str) -> str | None:
        """The cached token for `scope` while it has not expired; a lookup that never blocks."""
        entry = self._cache.get(scope)
        return entry[0] if entry is not None and entry[1] > self._clock() else None

    def fresh(self, scope: str) -> bool:
        """A cached token for `scope` outside the refresh margin (a lookup: never blocks)."""
        entry = self._cache.get(scope)
        return entry is not None and entry[1] - self._clock() > REFRESH_MARGIN_SECONDS

    def bearer(self, scope: str) -> str:
        """The token for `scope`, fetched from Entra (blocking) when the cache has none fresh."""
        entry = self._cache.get(scope)
        if entry is not None and entry[1] - self._clock() > REFRESH_MARGIN_SECONDS:
            return entry[0]
        with self._create:  # created on first use, so a key-only setup never needs it
            if self._credential is None:
                self._credential = default_credential()
        fetched = self._credential.get_token(scope)
        if not fetched.token:
            raise ValueError("the Entra credential returned an empty token")
        self._cache[scope] = (fetched.token, fetched.expires_on)
        return fetched.token

    def __repr__(self) -> str:  # tokens live only in `_cache`; never print it
        return "EntraTokens()"
