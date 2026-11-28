"""Entra ID bearer tokens for the Azure adapters: a cached, injectable token source.

`azure-identity` is an optional extra, so it is imported only when no credential object was
injected, and only here. A fetch is a blocking network call to Entra (typically 100 ms to a few
seconds), so an adapter's async `prepare` makes it in a worker thread (`providers.refresh`) and
`translate` only reads the cache: with no usable token there it raises (nothing is sent), and
never fetches on the event loop. The cache makes fetches rare: about one per token lifetime.
"""

import threading
import time
from collections.abc import Callable
from typing import Protocol

# Refresh this long before expiry so a request never leaves with a token about to lapse. A token
# issued with less than twice this much life is refreshed half way through it instead, so a
# short-lived token is reused for a while rather than fetched again on every call.
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


class ExpiredTokenError(ValueError):
    """The credential handed out a token whose expiry is not in the future (clock skew, a broken
    identity endpoint). It is refused and nothing is cached: the call fails closed and the next
    one asks again. The class name is what the internal-error log shows."""


class TokenNotReadyError(RuntimeError):
    """`translate` found no unexpired token: only `prepare` fetches one, off the event loop."""


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
        # scope -> (token, expires_on, refresh_at); never printed
        self._cache: dict[str, tuple[str, float, float]] = {}

    def cached(self, scope: str) -> str | None:
        """The cached token for `scope` while it has not expired; a lookup that never blocks."""
        entry = self._cache.get(scope)
        return entry[0] if entry is not None and entry[1] > self._clock() else None

    def ready(self, scope: str) -> str:
        """What `prepare` readied; raises (fail closed, nothing sent) rather than fetch inline."""
        token = self.cached(scope)
        if token is None:
            raise TokenNotReadyError("no Entra token is ready; prepare fetches one")
        return token

    def fresh(self, scope: str) -> bool:
        """A cached token for `scope` not yet due for refresh (a lookup: never blocks)."""
        entry = self._cache.get(scope)
        return entry is not None and self._clock() < entry[2]

    def fetch(self, scope: str) -> str:
        """A new token for `scope` from Entra (blocking: run it in a worker thread)."""
        with self._create:  # created on first use, so a key-only setup never needs it
            if self._credential is None:
                self._credential = default_credential()
        fetched = self._credential.get_token(scope)
        now = self._clock()
        if not fetched.token:
            self._cache.pop(scope, None)
            raise ValueError("the Entra credential returned an empty token")
        life = fetched.expires_on - now
        if life <= 0:
            self._cache.pop(scope, None)  # treated as no token at all
            raise ExpiredTokenError("the Entra credential returned an expired token")
        margin = min(REFRESH_MARGIN_SECONDS, life / 2)
        self._cache[scope] = (fetched.token, fetched.expires_on, fetched.expires_on - margin)
        return fetched.token

    def drop(self) -> None:
        """Forget every cached token (the provider rejected one): the next `prepare` fetches."""
        self._cache.clear()

    def __repr__(self) -> str:  # tokens live only in `_cache`; never print it
        return "EntraTokens()"
