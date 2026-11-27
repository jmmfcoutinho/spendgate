"""Load spendgate.yaml: read, validate, resolve secrets and the catalog, cross-check.

Fail closed: anything wrong raises one `ConfigError` listing every problem as `path: message`
(dotted path inside the file, plus the file name). Secret values never appear in a message.
`check_config` runs the same checks and reports instead of raising.
"""

import hashlib
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

import yaml
from pydantic import ValidationError

from spendgate.config.schema import Config, DeploymentSpec, Provider
from spendgate.config.secrets import ADC, Secret, check_ref, resolve
from spendgate.config.settings import (
    CheckReport,
    ResolvedDeployment,
    Route,
    Settings,
    WebhookTarget,
    is_vertex,
)
from spendgate.config.yamlio import load_yaml_bytes
from spendgate.core.catalog import Catalog, CatalogError, ModelNotPriced, parse
from spendgate.core.money import FxSnapshot, MissingFxRate, MoneyError
from spendgate.core.pepper import MIN_PEPPER_BYTES

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})  # hostnames as `urlsplit` gives them
_HINT = " (price it in a catalog.paths file, or draft a stand-in with `spendgate catalog draft`)"


class ConfigError(Exception):
    """The configuration is invalid; the process must not start. `issues` is (path, message)."""

    def __init__(self, file: str, issues: list[tuple[str, str]]) -> None:
        self.file = file
        # Deployments that share a provider resolve its credential once each: one line per problem.
        self.issues = tuple(dict.fromkeys(issues))
        super().__init__(
            "; ".join(f"{file}: {p}: {m}" if p else f"{file}: {m}" for p, m in self.issues)
        )


Issues = list[tuple[str, str]]


@dataclass(slots=True)
class _Ctx:
    """What every check shares: the environment and the problems found so far."""

    env: Mapping[str, str]
    issues: Issues
    unpriced: list[str]


def _yaml_mapping(data: bytes, name: str) -> dict[str, object]:
    try:
        doc = load_yaml_bytes(data)
    except yaml.YAMLError as exc:
        # Never str(exc): it quotes document lines. Problem text and position only.
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise ConfigError(name, [("", f"{problem}{where}")]) from None
    if not isinstance(doc, dict) or not all(isinstance(k, str) for k in doc):
        raise ConfigError(name, [("", "the document must be a mapping with text keys")])
    return {str(k): v for k, v in doc.items()}


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ConfigError(str(path), [("", f"cannot read file ({type(exc).__name__})")]) from None


def _validate(raw: dict[str, object], name: str) -> Config:
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        issues = [
            (".".join(str(p) for p in e["loc"]), e["msg"].removeprefix("Value error, "))
            for e in exc.errors(include_input=False, include_url=False)
        ]
        raise ConfigError(name, issues) from None


def read_config(path: str | Path) -> Config:
    """The file checked against the schema only: no secrets, catalog or cross-checks.

    For a client that needs a setting (the admin listener) without the server's environment.
    """
    name = str(path)
    return _validate(_yaml_mapping(_read(Path(path)), name), name)


def _catalog_documents(cfg: Config, base: Path) -> tuple[tuple[Mapping[str, object], ...], str]:
    """Bundled catalog first, then override files; hash covers every file's bytes in order."""
    blobs = [(resources.files("spendgate.catalog") / "prices.yaml").read_bytes()]
    names = ["bundled prices.yaml"]
    for p in cfg.catalog.paths:
        blobs.append(_read(base / p))
        names.append(p)
    digest = hashlib.sha256()
    docs = []
    for blob, name in zip(blobs, names, strict=True):
        digest.update(len(blob).to_bytes(8, "big"))  # length prefix: no ambiguity at file joins
        digest.update(blob)
        docs.append(_yaml_mapping(blob, f"catalog {name}"))
    return tuple(docs), digest.hexdigest()


def _fx(cfg: Config) -> tuple[FxSnapshot, ...]:
    if cfg.fx is None:
        return ()
    issues: Issues = []
    out = []
    for currency, rate in cfg.fx.rates.items():
        if currency == cfg.ledger.currency:
            issues.append((f"fx.rates.{currency}", "is the ledger currency; give rates for others"))
            continue
        try:
            out.append(
                FxSnapshot.of(
                    currency, cfg.ledger.currency, Decimal(rate), cfg.fx.as_of, cfg.fx.source
                )
            )
        except MoneyError as exc:
            issues.append((f"fx.rates.{currency}", str(exc)))
    if issues:
        raise ConfigError("fx", issues)
    return tuple(out)


def _secret(ref: str | None, path: str, ctx: _Ctx) -> Secret | None:
    if ref is None:
        return None
    try:
        return resolve(ref, ctx.env)
    except LookupError as exc:
        ctx.issues.append((path, str(exc)))
        return None


def _pepper(ref: str, ctx: _Ctx) -> Secret | None:
    """The key pepper, long enough to key the stored hashes; the error never shows its value."""
    pepper = _secret(ref, "server.key_pepper", ctx)
    if pepper is not None and len(pepper.reveal().encode()) < MIN_PEPPER_BYTES:
        hint = "e.g. `openssl rand -hex 32`"
        ctx.issues.append(
            ("server.key_pepper", f"must be at least {MIN_PEPPER_BYTES} bytes ({hint})")
        )
        return None
    return pepper


def _base_url_issue(url: str) -> str | None:
    scheme = url.partition("://")[0]
    plain = url.isprintable() and not any(c.isspace() for c in url)  # urlsplit drops tabs, CR, LF
    try:  # the host as the HTTP client parses it: an `@` after a `?` or `#` is not userinfo
        host = (urlsplit(url).hostname or "") if plain else ""
    except ValueError:  # e.g. an unclosed `[` in an IPv6 literal
        host = ""
    if scheme == "https" and host:
        return None
    if scheme == "http" and host in _LOOPBACK:
        return None  # plain HTTP only to this machine (tests, sidecars)
    return "must be an https:// URL (http:// only for localhost)"


def _webhook(cfg: Config, ctx: _Ctx) -> WebhookTarget | None:
    hook = cfg.alerts.webhook
    if hook is None:
        return None
    url = _secret(hook.url, "alerts.webhook.url", ctx)
    key = _secret(hook.signing_secret, "alerts.webhook.signing_secret", ctx)
    # The same rule as a provider base_url; the message never quotes the URL.
    if url is not None and (problem := _base_url_issue(url.reveal())) is not None:
        ctx.issues.append(("alerts.webhook.url", problem))
        return None
    # The HMAC key is the secret's UTF-8 bytes. A non-UTF-8 environment value arrives as
    # surrogate escapes (the only text UTF-8 cannot encode) and would fail only when signing.
    if key is not None and any("\ud800" <= c <= "\udfff" for c in key.reveal()):
        ctx.issues.append(("alerts.webhook.signing_secret", "must be UTF-8 text"))
        return None
    if url is None or key is None:
        return None
    return WebhookTarget(url, key, hook.timeout_seconds, hook.max_attempts, hook.queue_size)


def _deployment(
    spec: DeploymentSpec, prov: Provider, model_part: str, at: str, ctx: _Ctx
) -> ResolvedDeployment | None:
    issues = ctx.issues
    base_url = spec.base_url or prov.base_url
    if base_url is not None and base_url.startswith("env:"):
        where_url = f"{at}.base_url" if spec.base_url else f"providers.{spec.provider}.base_url"
        try:
            check_ref(base_url)
            base_url = resolve(base_url, ctx.env).reveal()  # a URL, not a secret: keep it plain
        except (ValueError, LookupError) as exc:
            issues.append((where_url, str(exc)))
            return None
    auth = spec.auth or prov.auth
    ref = spec.credential or prov.credential
    if base_url is None:
        issues.append((at, f"no base_url (set it here or in providers.{spec.provider})"))
    elif (problem := _base_url_issue(base_url)) is not None:
        issues.append((f"{at}.base_url", problem))
    if auth != "sigv4" and ref is None:
        issues.append((at, f"auth {auth} needs a credential (env:NAME or file:/path)"))
    where = credential_path(spec, at)
    if ref == ADC and (spec.provider != "google" or auth != "bearer_token"):
        issues.append((where, "adc is for Google Vertex AI only (auth bearer_token)"))
    credential = None if ref == ADC else _secret(ref, where, ctx)
    if base_url is None:
        return None
    return ResolvedDeployment(
        spec.provider,
        base_url,
        spec.model or model_part,
        auth,
        spec.region or prov.region,
        spec.api_version or prov.api_version,
        credential,
        ref == ADC,
    )


def credential_path(spec: DeploymentSpec, at: str) -> str:
    """Where the deployment's credential reference is written: its own key or its provider's."""
    return f"{at}.credential" if spec.credential else f"providers.{spec.provider}.credential"


def _priced(
    catalog: Catalog, catalog_id: str, region: str | None, now_ms: int | None, listed_only: bool
) -> bool:
    provider, _, model = catalog_id.partition("/")
    if now_ms is None:  # date-independent: could any entry ever price this deployment?
        return catalog.could_price(provider, model, region, listed_only=listed_only)
    try:
        catalog.lookup(provider, model, now_ms, region, listed_only=listed_only)
    except ModelNotPriced:
        return False
    return True


class _Placement(NamedTuple):
    """Where a route's deployments are, and whether a region-less entry may price them."""

    regions: list[str | None]
    listed_only: bool  # a route with a Vertex AI deployment (ADR-0034)


def _price_issue(
    catalog: Catalog, catalog_id: str, where: _Placement, now_ms: int | None, ctx: _Ctx
) -> str | None:
    """Why the route's deployments do not share one price (ADR-0028, ADR-0034), or None."""
    regions, listed_only = where
    unpriced = [
        r
        for r in dict.fromkeys(regions)
        if not _priced(catalog, catalog_id, r, now_ms, listed_only)
    ]
    provider, _, model = catalog_id.partition("/")
    if unpriced:
        ctx.unpriced.append(catalog_id)
        if listed_only:
            return _vertex_unpriced(catalog_id, unpriced, catalog.listed_regions(provider, model))
        if len(unpriced) == len(set(regions)):
            return f"{catalog_id} has no price in the catalog{_HINT}"
        return f"{catalog_id} has no price in the catalog for {_places(unpriced)}{_HINT}"
    split = catalog.split_by_region(provider, model, regions, now_ms, listed_only=listed_only)
    if split is None:
        return None
    prices = ", ".join(f"{_places([r])} -> {e.price_id_in(r)}" for r, e in split.items())
    return (
        f"deployments resolve to different prices ({prices}); one route has one price, "
        "so route differently-priced Regions as separate models"
    )


def _vertex_unpriced(catalog_id: str, unpriced: list[str | None], listed: tuple[str, ...]) -> str:
    """A Vertex AI route never takes a region-less (Gemini API) price (ADR-0034)."""
    known = (
        f"Regions listed for that id: {', '.join(listed)}" if listed else "no entry of it lists one"
    )
    return (
        f"{catalog_id} has no Vertex AI price for {_places(unpriced)}: a Vertex deployment is "
        f"priced only by an entry that lists its Region, never by the region-less Gemini API "
        f"price ({known}){_HINT}"
    )


def _places(regions: list[str | None]) -> str:
    named = [f"region {r}" for r in regions if r is not None]
    return ", ".join(named + ["a deployment without a region"] * (None in regions))


def _routes(cfg: Config, catalog: Catalog, now_ms: int | None, ctx: _Ctx) -> dict[str, Route]:
    issues = ctx.issues
    routes: dict[str, Route] = {}
    for alias, route in cfg.models.items():
        at = f"models.{alias}"
        provider, _, model_part = route.catalog_id.partition("/")
        specs = [
            (s, cfg.providers.get(s.provider, Provider()))
            for s in route.deployments
            if s.provider == provider
        ]
        regions = [s.region or prov.region for s, prov in specs]
        listed_only = any(is_vertex(s.provider, s.auth or prov.auth) for s, prov in specs)
        where = _Placement(regions or [None], listed_only)
        problem = _price_issue(catalog, route.catalog_id, where, now_ms, ctx)
        if problem is not None:
            issues.append((f"{at}.catalog_id", problem))
        deployments = []
        for i, spec in enumerate(route.deployments):
            here = f"{at}.deployments.{i}"
            if spec.provider != provider:
                issues.append((f"{here}.provider", f"must be {provider} (same catalog id only)"))
                continue
            built = _deployment(
                spec, cfg.providers.get(provider, Provider()), model_part, here, ctx
            )
            if built is not None:
                deployments.append(built)
        routes[alias] = Route(alias, route.catalog_id, tuple(deployments), listed_only)
    return routes


def _bootstrap_issues(cfg: Config, catalog: Catalog, issues: Issues) -> None:
    b = cfg.bootstrap
    scopes = set(b.scopes)
    known_models = set(cfg.models) | {e.id for e in catalog.entries()}
    for kind, items in (("budgets", b.budgets), ("keys", b.keys)):
        ids = [getattr(x, "id", None) or getattr(x, "name", "") for x in items]
        for i, ident in enumerate(ids):
            if ids.index(ident) != i:
                issues.append((f"bootstrap.{kind}.{i}", f"duplicate {ident!r}"))
    for i, budget in enumerate(b.budgets):
        if budget.scope not in scopes:
            issues.append((f"bootstrap.budgets.{i}.scope", "not listed in bootstrap.scopes"))
        if budget.cap.currency != cfg.ledger.currency:
            issues.append((f"bootstrap.budgets.{i}.cap", f"must be in {cfg.ledger.currency}"))
    for i, key in enumerate(b.keys):
        if key.scope is not None and key.scope not in scopes:
            issues.append((f"bootstrap.keys.{i}.scope", "not listed in bootstrap.scopes"))
        for j, model in enumerate(key.models or ()):
            if model not in known_models:
                issues.append(
                    (f"bootstrap.keys.{i}.models.{j}", f"{model!r} is not a routed model")
                )
        sd = key.session_defaults
        if sd is not None and sd.budget is not None and sd.budget.currency != cfg.ledger.currency:
            issues.append(
                (f"bootstrap.keys.{i}.session_defaults.budget", f"must be in {cfg.ledger.currency}")
            )


def _ledger_file_issue(path: Path) -> str | None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None  # created by the ledger on first start with mode 0600
    except OSError as exc:
        return f"cannot stat ({type(exc).__name__})"
    if mode & 0o077:
        return f"file mode is {mode:04o}; must be 0600 (no group or other access)"
    return None


def _catalog(
    cfg: Config, base: Path, name: str
) -> tuple[tuple[Mapping[str, object], ...], str, tuple[FxSnapshot, ...], Catalog]:
    try:
        docs, sha = _catalog_documents(cfg, base)
        fx = _fx(cfg)
        return docs, sha, fx, parse(docs, sha, ledger_currency=cfg.ledger.currency, fx=fx)
    except ConfigError as exc:
        raise ConfigError(name, [(p or exc.file, m) for p, m in exc.issues]) from None
    except MissingFxRate as exc:
        raise ConfigError(
            name, [("fx", f"{exc} (required when a catalog currency differs from the ledger's)")]
        ) from None
    except CatalogError as exc:
        raise ConfigError(name, [("catalog", str(exc))]) from None


def read_catalog(path: str | Path) -> tuple[Catalog, tuple[Mapping[str, object], ...]]:
    """The effective catalog of the file (bundled, then `catalog.paths`) and its documents.

    Like `read_config`, no secret is resolved and nothing else is cross-checked: for an offline
    tool (`spendgate catalog draft`) that copies a price, not for serving.
    """
    p = Path(path)
    cfg = read_config(p)
    docs, _, _, catalog = _catalog(cfg, p.resolve().parent, str(p))
    return catalog, docs


def _build(path: Path, env: Mapping[str, str], now_ms: int | None, unpriced: list[str]) -> Settings:
    name = str(path)
    cfg = _validate(_yaml_mapping(_read(path), name), name)
    base = path.resolve().parent
    ctx = _Ctx(env, [], unpriced)
    issues = ctx.issues
    docs, sha, fx, catalog = _catalog(cfg, base, name)
    pepper = _pepper(cfg.server.key_pepper, ctx)
    backend = cfg.ledger.backend
    dsn = ledger_path = None
    if backend.kind == "postgres":
        dsn = _secret(backend.dsn, "ledger.backend.dsn", ctx)
    else:
        ledger_path = (base / backend.path).resolve()
        if (problem := _ledger_file_issue(ledger_path)) is not None:
            issues.append(("ledger.backend.path", problem))
    routes = _routes(cfg, catalog, now_ms, ctx)
    webhook = _webhook(cfg, ctx)
    _bootstrap_issues(cfg, catalog, issues)
    if issues or pepper is None:
        raise ConfigError(name, issues)
    return Settings(path, cfg, catalog, docs, sha, fx, pepper, dsn, ledger_path, routes, webhook)


def load(path: str | Path, *, env: Mapping[str, str], now_ms: int | None = None) -> Settings:
    """Validated settings, or `ConfigError`. `env` is the environment to resolve `env:` refs in.

    With `now_ms`, a routed model must be priced *at that time* (not merely present in the
    catalog), so an expired or deprecated entry also refuses to start.
    """
    return _build(Path(path), env, now_ms, [])


def check_config(
    path: str | Path, *, env: Mapping[str, str], now_ms: int | None = None
) -> CheckReport:
    """Run every start-up check without starting; list errors and unpriced routed models."""
    unpriced: list[str] = []
    try:
        settings = _build(Path(path), env, now_ms, unpriced)
    except ConfigError as exc:
        errors = tuple(f"{p}: {m}" if p else m for p, m in exc.issues)
        return CheckReport(errors, tuple(sorted(set(unpriced))))
    return CheckReport(
        (),
        (),
        settings.unverified_summary(),
        tuple(settings.unmatched_filters()),
        settings.superseded_summary(),
        tuple(settings.unlisted_regions()),
    )
