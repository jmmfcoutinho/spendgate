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
    """Bearer tokens per scope, cached until shortly before they expire (thread-safe)."""

    def __init__(
        self,
        credential: TokenCredential | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._credential = credential
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[str, int]] = {}  # scope -> (token, expires_on)

    def fresh(self, scope: str) -> bool:
        """A cached token for `scope` outside the refresh margin (a lookup: never blocks)."""
        cached = self._cache.get(scope)
        return cached is not None and cached[1] - self._clock() > REFRESH_MARGIN_SECONDS

    def bearer(self, scope: str) -> str:
        """The token for `scope`, fetched from Entra (blocking) when the cache has none fresh."""
        with self._lock:
            cached = self._cache.get(scope)
            if cached and cached[1] - self._clock() > REFRESH_MARGIN_SECONDS:
                return cached[0]
            if self._credential is None:  # created on first use, so a key-only setup never needs it
                self._credential = default_credential()
            fresh = self._credential.get_token(scope)
            if not fresh.token:
                raise ValueError("the Entra credential returned an empty token")
            self._cache[scope] = (fresh.token, fresh.expires_on)
            return fresh.token

    def __repr__(self) -> str:  # tokens live only in `_cache`; never print it
        return "EntraTokens()"
