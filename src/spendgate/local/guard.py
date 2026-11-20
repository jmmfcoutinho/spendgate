"""`guard()`: the one-call quick start for scripts and notebooks.

    guard = spendgate.local.guard(cap="5 USD")
    client = guard.openai()          # an OpenAI SDK client whose calls are capped at 5 USD

With no config file, routes are built from the catalog (bundled, plus `catalog=`) for every
provider whose credential is available: `OPENAI_API_KEY` (or `provider_keys={"openai": ...}`) and
`ANTHROPIC_API_KEY` (or `provider_keys={"anthropic": ...}`). Other providers need a config file.
"""

import json
import tempfile
from collections.abc import Mapping, Sequence
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, overload

import httpx

from spendgate.config import ConfigError, Settings, load
from spendgate.config.yamlio import load_yaml_bytes
from spendgate.core.money import FxSnapshot, Money, parse_amount
from spendgate.local import sdk as sdk_clients
from spendgate.local.runtime import PEPPER_VAR, local_env
from spendgate.local.setup import default_ledger_path
from spendgate.local.transport import AsyncLocalTransport, LocalTransport, transport
from spendgate.pipeline.model import Clock
from spendgate.pipeline.pipeline import system_ms

if TYPE_CHECKING:
    import anthropic as _anthropic
    import openai as _openai

    from spendgate.local.transport_httpx2 import AsyncLocalTransport2, LocalTransport2

# provider slug -> (credential env var, API base URL). Others (Azure, Bedrock, Google) need a
# config file because they need a resource name or region as well.
PROVIDERS = {
    "openai": ("OPENAI_API_KEY", "https://api.openai.com"),
    "anthropic": ("ANTHROPIC_API_KEY", "https://api.anthropic.com"),
}


class GuardError(ValueError):
    """`guard()` cannot start: a message says what to give it."""


def _catalog_ids(extra: Path | None) -> list[str]:
    """Model ids of the bundled catalog, then `extra`, in file order, without repeats."""
    blobs = [(resources.files("spendgate.catalog") / "prices.yaml").read_bytes()]
    if extra is not None:
        blobs.append(extra.read_bytes())
    ids: dict[str, None] = {}
    for blob in blobs:
        doc = load_yaml_bytes(blob)
        entries = doc.get("models", []) if isinstance(doc, dict) else []
        ids.update((e["id"], None) for e in entries if isinstance(e, dict) and "id" in e)
    return [i for i in ids if isinstance(i, str)]


def _fx_section(fx: FxSnapshot | Sequence[FxSnapshot] | None, currency: str) -> dict[str, object]:
    snapshots = [fx] if isinstance(fx, FxSnapshot) else list(fx or ())
    if not snapshots:
        return {}
    first = snapshots[0]
    for s in snapshots:
        if s.to_currency != currency:
            raise GuardError(f"fx rates must convert into the ledger currency {currency}")
        if (s.as_of, s.source) != (first.as_of, first.source):
            raise GuardError("all fx rates must share one as_of date and source")
    rates = {s.from_currency: format(s.rate, "f") for s in snapshots}
    return {"fx": {"rates": rates, "as_of": first.as_of, "source": first.source}}


def _config(
    cap: Money, ledger_path: Path, catalog: Path | None,
    fx: FxSnapshot | Sequence[FxSnapshot] | None, present: Mapping[str, str],
) -> dict[str, object]:  # fmt: skip
    models: dict[str, object] = {}
    for catalog_id in _catalog_ids(catalog):
        slug, _, name = catalog_id.partition("/")
        if slug in present:
            models.setdefault(name, {"catalog_id": catalog_id, "deployments": [{"provider": slug}]})
    providers = {
        slug: {"base_url": PROVIDERS[slug][1], "credential": f"env:{var}"}
        for slug, var in present.items()
    }
    return {
        "server": {"listen": "127.0.0.1:0", "key_pepper": f"env:{PEPPER_VAR}"},
        "ledger": {
            "currency": cap.currency, "backend": {"kind": "sqlite", "path": str(ledger_path)},
        },
        "providers": providers, "models": models,
        "catalog": {"paths": [str(catalog)] if catalog else []},
        **_fx_section(fx, cap.currency),
    }  # fmt: skip


def _load_synthesized(
    config: dict[str, object], env: Mapping[str, str], wants_fx: bool, cap: Money
) -> Settings:
    with tempfile.TemporaryDirectory(prefix="spendgate-") as tmp:
        path = Path(tmp) / "spendgate-local.yaml"
        path.write_text(json.dumps(config))  # JSON is YAML; no secret in it, only env references
        try:
            return load(path, env=env)
        except ConfigError as exc:
            fx_issues = [m for p, m in exc.issues if p.startswith("fx")]
            if fx_issues and not wants_fx:
                rate = fx_issues[0].split(" (")[0]
                raise GuardError(
                    f"cap {cap.format()} is not in the catalog's currency: {rate}. Pass "
                    f"fx=FxSnapshot.of('USD', '{cap.currency}', rate, 'YYYY-MM-DD', 'source')."
                ) from None
            raise


def _synthesize(
    cap: Money, catalog: str | Path | None, ledger_path: Path,
    fx: FxSnapshot | Sequence[FxSnapshot] | None, provider_keys: Mapping[str, str] | None,
) -> Settings:  # fmt: skip
    keys = dict(provider_keys or {})
    unknown = set(keys) - set(PROVIDERS)
    if unknown:
        raise GuardError(f"provider_keys supports {sorted(PROVIDERS)}, not {sorted(unknown)}")
    env = local_env()
    present: dict[str, str] = {}
    for slug, (var, _url) in PROVIDERS.items():
        if slug in keys:
            env[var] = keys[slug]
        if env.get(var):
            present[slug] = var
    if not present:
        names = " or ".join(var for var, _ in PROVIDERS.values())
        raise GuardError(f"no provider credentials: set {names}, pass provider_keys=, or config=")
    extra = Path(catalog).resolve() if catalog is not None else None
    config = _config(cap, ledger_path, extra, fx, present)
    return _load_synthesized(config, env, fx is not None, cap)


class _Closer(Protocol):
    def close(self) -> None: ...


class _AsyncCloser(Protocol):
    async def aclose(self) -> None: ...


class Guard:
    """A guarded ledger plus factories for clients that spend from it."""

    def __init__(
        self, settings: Settings, ledger_path: Path, cap: str, *,
        client: httpx.AsyncClient | None = None, now_ms: Clock = system_ms,
    ) -> None:  # fmt: skip
        self.settings, self.ledger_path, self.cap = settings, ledger_path, cap
        self._client, self._now = client, now_ms
        self._sync: list[_Closer] = []
        self._async: list[_AsyncCloser] = []

    @overload
    def transport(self, *, async_: Literal[False] = ...) -> LocalTransport: ...
    @overload
    def transport(self, *, async_: Literal[True]) -> AsyncLocalTransport: ...
    def transport(self, *, async_: bool = False) -> LocalTransport | AsyncLocalTransport:
        """A new `httpx` transport on this ledger, for httpx-based clients.

        The current OpenAI and Anthropic SDKs use `httpx2` and need `openai()`/`anthropic()`.
        Each transport has its own ledger connection (and, if sync, its own loop thread).
        """
        if async_:
            made = transport(
                self.settings, ledger_path=self.ledger_path, cap=self.cap, client=self._client,
                now_ms=self._now, async_=True,
            )  # fmt: skip
            self._async.append(made)
            return made
        sync = transport(
            self.settings, ledger_path=self.ledger_path, cap=self.cap, client=self._client,
            now_ms=self._now,
        )  # fmt: skip
        self._sync.append(sync)
        return sync

    def _sync2(self) -> "LocalTransport2":
        from spendgate.local.transport_httpx2 import transport2  # noqa: PLC0415 - needs httpx2

        made = transport2(
            self.settings, ledger_path=self.ledger_path, cap=self.cap, client=self._client,
            now_ms=self._now,
        )  # fmt: skip
        self._sync.append(made)
        return made

    def _async2(self) -> "AsyncLocalTransport2":
        from spendgate.local.transport_httpx2 import transport2  # noqa: PLC0415 - needs httpx2

        made = transport2(
            self.settings, ledger_path=self.ledger_path, cap=self.cap, client=self._client,
            now_ms=self._now, async_=True,
        )  # fmt: skip
        self._async.append(made)
        return made

    @overload
    def openai(self, *, async_: Literal[False] = ..., **sdk: object) -> "_openai.OpenAI": ...
    @overload
    def openai(self, *, async_: Literal[True], **sdk: object) -> "_openai.AsyncOpenAI": ...
    def openai(
        self, *, async_: bool = False, **sdk: object
    ) -> "_openai.OpenAI | _openai.AsyncOpenAI":
        """An OpenAI SDK client (needs the `openai` extra) that spends from this guard."""
        if async_:
            return sdk_clients.openai_async(self._async2(), sdk)
        return sdk_clients.openai_sync(self._sync2(), sdk)

    @overload
    def anthropic(
        self, *, async_: Literal[False] = ..., **sdk: object
    ) -> "_anthropic.Anthropic": ...
    @overload
    def anthropic(self, *, async_: Literal[True], **sdk: object) -> "_anthropic.AsyncAnthropic": ...
    def anthropic(
        self, *, async_: bool = False, **sdk: object
    ) -> "_anthropic.Anthropic | _anthropic.AsyncAnthropic":
        """An Anthropic SDK client (needs the `anthropic` extra) that spends from this guard."""
        if async_:
            return sdk_clients.anthropic_async(self._async2(), sdk)
        return sdk_clients.anthropic_sync(self._sync2(), sdk)

    def close(self) -> None:
        """Close every sync transport this guard made (async ones: `await aclose()`)."""
        for t in self._sync:
            t.close()
        self._sync.clear()

    async def aclose(self) -> None:
        for t in self._async:
            await t.aclose()

    def __enter__(self) -> "Guard":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def guard(  # noqa: PLR0913 - the quick start's documented options
    cap: str = "5 USD", catalog: str | Path | None = None, *, ledger_path: str | Path | None = None,
    fx: FxSnapshot | Sequence[FxSnapshot] | None = None,
    provider_keys: Mapping[str, str] | None = None, config: str | Path | None = None,
    client: httpx.AsyncClient | None = None, now_ms: Clock = system_ms,
) -> Guard:  # fmt: skip
    """Open (or create) the local ledger with a lifetime budget of `cap` and return a `Guard`.

    The ledger is a SQLite file shared by every script that names the same path; the default is
    `default_ledger_path()` (0600, in a 0700 folder). The cap is set once: reusing the file with
    another cap is an error, never a silent change. The ledger currency is the cap's currency; a
    cap that differs from the catalog's (USD) needs `fx=`. `config=` takes a `spendgate.yaml`
    instead of building routes from the environment (it must use the same currency as `cap`).
    """
    amount = parse_amount(cap)
    if amount.nano <= 0:
        raise GuardError("cap must be greater than zero")
    path = Path(ledger_path) if ledger_path is not None else default_ledger_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if config is not None:
        if catalog is not None or fx is not None or provider_keys is not None:
            raise GuardError("config= already names the catalog, fx and credentials; drop those")
        settings = load(config, env=local_env())
        if settings.ledger_currency != amount.currency:
            raise GuardError(
                f"{config} has ledger currency {settings.ledger_currency}, not {amount.currency}"
            )
    else:
        settings = _synthesize(amount, catalog, path, fx, provider_keys)
    # Start one transport and drop it: scope and budget are created now, so a bad cap, a wrong
    # currency or a mismatched ledger fails here and not on the first request.
    transport(settings, ledger_path=path, cap=cap, client=client, now_ms=now_ms).close()
    return Guard(settings, path, cap, client=client, now_ms=now_ms)
