"""Generate `docs/configuration.md` from the Pydantic models in `spendgate.config.schema`.

    uv run python scripts/gen_config_docs.py            # rewrite docs/configuration.md
    uv run python scripts/gen_config_docs.py --check    # exit 1 if the committed file is stale

Keys, types and defaults come from the models. Descriptions are kept here, keyed by
`Model.field`, because the models carry comments rather than descriptions. A key without a
description fails the run, so a new setting cannot ship undocumented, and a test compares the
committed file with this output, so the reference cannot drift from the code.
"""

import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from annotated_types import Ge, Gt, Le
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from spendgate.config import schema
from spendgate.config.secrets import check_ref
from spendgate.core.money import Money

OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "configuration.md"

D: dict[str, str] = {
    # Config
    "Config.server": "The proxy's listeners and the key pepper.",
    "Config.ledger": "Where money state lives, its currency, and hold timing.",
    "Config.fx": (
        "Exchange rates into the ledger currency. Required when any catalog entry is in another "
        "currency than `ledger.currency`; startup fails without it."
    ),
    "Config.catalog": "Price catalog overrides on top of the bundled catalog.",
    "Config.providers": (
        "Connection defaults per provider slug, used by every deployment of that provider "
        "unless the deployment overrides them."
    ),
    "Config.models": (
        "Routable model names. Each alias maps to one catalog id and an ordered list of "
        "deployments; failover happens only between deployments of that id."
    ),
    "Config.defaults": "Policy defaults for keys and budgets that do not set their own.",
    "Config.fail_open": (
        "Explicit, named opt-outs from fail-closed behaviour. Every enabled one is logged as "
        "a warning at start."
    ),
    "Config.bootstrap": (
        "Scopes, budgets and keys created at start. Create-only and idempotent: an existing "
        "row is never changed by a restart. `serve` creates scopes and budgets only; keys are "
        "created by `spendgate migrate --bootstrap`, which prints each new secret once."
    ),
    "Config.anthropic": "Request header allowlists for the Anthropic endpoint.",
    "Config.routing": "Failover and cooldown behaviour across deployments of one model.",
    # Server
    "Server.listen": "Data-plane address, `host:port`.",
    "Server.admin": (
        "Optional separate listener for `/admin/v1` and `/metrics`. Without it they share the "
        "data port, and `/metrics` then needs an admin key."
    ),
    "Server.max_body_bytes": (
        "Largest accepted audio upload (`/v1/audio/transcriptions`, multipart) in bytes "
        "(default 25 MiB). Larger requests are refused with `request_too_large` before the body "
        "is read in full."
    ),
    "Server.max_json_body_bytes": (
        "Largest accepted JSON request body in bytes (default 4 MiB). JSON parses into about 25 "
        "times its size in memory, so it has a smaller cap than an upload. Larger requests are "
        "refused with `request_too_large` before the body is read."
    ),
    "Server.body_read_timeout_seconds": (
        "Longest a client may take to send its whole request body (default 30). A slower or "
        "stalled upload is refused with HTTP 408 and frees its place at once."
    ),
    "Server.max_request_seconds": (
        "Longest one call or stream may run, end to end (default 900). At the limit the gateway "
        "stops it and settles it as `timeout` (buffered) or `stream_broken` (stream): the actual "
        "usage if seen, else the full hold."
    ),
    "Server.max_concurrent_requests": (
        "Most requests being admitted at once; beyond it the proxy refuses fast. It covers "
        "intake only: a running stream is limited by the key's concurrency."
    ),
    "Server.key_pepper": (
        "Secret used to hash stored key secrets. Keep it: changing it invalidates every key."
    ),
    "AdminServer.listen": "Admin address, `host:port`. Keep it on loopback or an internal network.",
    # Ledger
    "Ledger.backend": "`sqlite` (one host) or `postgres` (shared use); see the two tables below.",
    "Ledger.currency": "ISO 4217 code of every budget and every recorded cost.",
    "Ledger.lease_seconds": (
        "How long a hold lives without a heartbeat. A hold whose owner died is charged in full "
        "by the sweeper after this time."
    ),
    "Ledger.heartbeat_seconds": (
        "How often a running call renews its hold. Default is `lease_seconds / 3`; it must be "
        "shorter than `lease_seconds`."
    ),
    "Ledger.sweep_seconds": "How often the server charges and releases expired holds.",
    "Ledger.retention_days": (
        "Calls older than this many whole UTC days are deleted once a day. Usage reports cover "
        "only this window."
    ),
    "SqliteLedger.kind": "Always `sqlite`.",
    "SqliteLedger.path": (
        "Database file. A relative path is relative to the config file. The file must be mode "
        "0600 and is created that way."
    ),
    "PostgresLedger.kind": "Always `postgres`. Needs the `postgres` extra.",
    "PostgresLedger.dsn": "Secret reference to the connection URL, password included.",
    # Fx
    "Fx.rates": (
        "Map from currency code to rate: 1 unit of that currency equals this many ledger units, "
        'as a quoted decimal such as `"0.92"`.'
    ),
    "Fx.as_of": "Date of the rate, `YYYY-MM-DD`. Recorded with every call.",
    "Fx.source": "Where the rate came from, for the audit trail.",
    # CatalogPaths
    "CatalogPaths.paths": (
        "Override files in the catalog format, applied in order after the bundled catalog. "
        "An entry replaces the bundled one with the same id and `effective_from`."
    ),
    # Provider / DeploymentSpec
    "Provider.base_url": (
        "Provider API root. `https` only, or `http` for loopback. May be `env:NAME`, read at start."
    ),
    "Provider.auth": "How the proxy authenticates to the provider.",
    "Provider.credential": (
        "Secret reference for the key or token. Not used with `sigv4`, which reads the standard "
        "AWS credential chain."
    ),
    "Provider.region": "Provider region, where the adapter needs one (Bedrock, Azure).",
    "Provider.api_version": "Azure OpenAI query version; leave unset for the v1 path.",
    "DeploymentSpec.provider": "Provider slug. It must match the provider part of `catalog_id`.",
    "DeploymentSpec.model": (
        "The provider's own model or deployment name. Default: the model part of `catalog_id`."
    ),
    "DeploymentSpec.base_url": (
        "Overrides `providers.<slug>.base_url` for this deployment. May be `env:NAME`."
    ),
    "DeploymentSpec.auth": "Overrides `providers.<slug>.auth` for this deployment.",
    "DeploymentSpec.credential": "Overrides `providers.<slug>.credential` for this deployment.",
    "DeploymentSpec.region": "Overrides `providers.<slug>.region` for this deployment.",
    "DeploymentSpec.api_version": "Overrides `providers.<slug>.api_version` for this deployment.",
    # ModelRoute
    "ModelRoute.catalog_id": (
        "Catalog id, `provider/model`. Its price entry prices every deployment; no entry in "
        "force means `model_not_priced`."
    ),
    "ModelRoute.deployments": (
        "Candidates in preference order. A throttled or unreachable one is skipped for the "
        "next, at no cost."
    ),
    # Limits
    "Limits.rpm": "Requests per minute. Unset means no limit.",
    "Limits.tpm": "Estimated tokens per minute. Unset means no limit.",
    "Limits.concurrency": "Calls in flight at once. Unset means no limit.",
    # Defaults
    "Defaults.limits": "Applied to a key for each limit the key does not set itself.",
    "Defaults.max_output_tokens": (
        "Output ceiling for keys with none of their own. If a request, the key and this all "
        "leave it open, the catalog entry's `limits.max_output_tokens` applies."
    ),
    "Defaults.max_embedding_items": "Most inputs accepted in one embeddings request.",
    "Defaults.strict_overrun": (
        "When a settlement exceeds the hold and breaches the cap, block the budget until an "
        "operator runs `budgets ack-overrun`. A budget can override this."
    ),
    # FailOpen
    "FailOpen.allow_remote_media": (
        "Accept image, audio and file URLs in request bodies. The provider then fetches them "
        "itself, which is a server-side request forgery risk. Off by default: only inline "
        "`data:` URLs are accepted."
    ),
    # Routing
    "Routing.max_attempts": "Most deployments tried for one request.",
    "Routing.cooldown_seconds": (
        "How long a deployment is skipped after the provider throttles it (429) or its circuit "
        "breaker opens. A connect failure alone does not cool a deployment. Must not exceed "
        "`max_cooldown_seconds`. A `Retry-After` header is honoured."
    ),
    "Routing.failure_threshold_pct": (
        "Failure percentage in the window that puts a deployment into cooldown."
    ),
    "Routing.min_requests": "Requests in the window before the failure percentage counts.",
    "Routing.window_seconds": "Length of the failure window.",
    "Routing.max_cooldown_seconds": "Upper bound on any cooldown.",
    "Routing.connect_timeout_seconds": (
        "How long to wait for the TCP/TLS connection to the provider (DNS included) before the "
        "attempt counts as unreachable. Read and total timeouts are not affected."
    ),
    # Anthropic
    "Anthropic.allowed_versions": "`anthropic-version` header values the proxy accepts.",
    "Anthropic.allowed_betas": "`anthropic-beta` header values the proxy accepts. Empty: none.",
    # Bootstrap
    "Bootstrap.scopes": "Scope paths to create, for example `org:acme/project:app`.",
    "Bootstrap.budgets": "Budgets to create. Each scope on the path must exist or be listed.",
    "Bootstrap.keys": "Keys to create: data keys for apps, admin keys for operators.",
    "BootstrapBudget.id": "Stable name; stored as `bootstrap-<id>`. Never recreated once seen.",
    "BootstrapBudget.scope": "Scope path the budget applies to. It must be in `bootstrap.scopes`.",
    "BootstrapBudget.period": "`none` is a lifetime cap; `day` and `month` reset at UTC.",
    "BootstrapBudget.cap": 'Quoted amount in the ledger currency, such as `"50 EUR"`.',
    "BootstrapBudget.soft_pct": "Percentage of the cap that raises a warning, never a refusal.",
    "BootstrapBudget.model_filter": "Apply the budget only to calls of matching models.",
    "BootstrapBudget.strict_overrun": "Overrides `defaults.strict_overrun` for this budget.",
    "BootstrapKey.name": "Label of the key. A name already in use is left alone.",
    "BootstrapKey.scope": "Scope path of a data key. Give this or `admin_scopes`, not both.",
    "BootstrapKey.admin_scopes": "Makes an admin key with these permissions. Give this or `scope`.",
    "BootstrapKey.models": ("Allow list of aliases or catalog ids. Unset: every routed model."),
    "BootstrapKey.limits": "Rate and concurrency limits of this key.",
    "BootstrapKey.max_output_tokens": "Output ceiling of this key.",
    "BootstrapKey.session_defaults": (
        "Limits for sessions on this key. A request that names a session needs them."
    ),
    "SessionDefaultsSpec.budget": "Cap of one session, inside the shared budgets.",
    "SessionDefaultsSpec.max_steps": "Most requests in one session.",
    "SessionDefaultsSpec.max_seconds": "Wall-clock limit from the first call of a session.",
    "SessionDefaultsSpec.idle_seconds": (
        "A session idle this long is closed and the same id starts a fresh one."
    ),
    "SessionDefaultsSpec.max_new_sessions_per_hour": "Bounds how fast a key can start sessions.",
}

# Type text for the validated aliases, recognised by their constraints.
_PATTERNS = {
    "^[A-Z]{3}$": "currency code",
    r"^[A-Za-z0-9.\-:\[\]]+:[0-9]{1,5}$": "`host:port`",
    "^[a-z][a-z0-9_]*/.+$": "catalog id `provider/model`",
    "^[A-Za-z0-9][A-Za-z0-9._-]*$": "name",
    r"^\d{4}-\d{2}-\d{2}$": "date `YYYY-MM-DD`",
}


def _constraints(meta: list[Any]) -> tuple[str | None, str | None]:
    """Pattern text and a numeric bound, from a field's metadata."""
    pattern = bound = None
    low = high = None
    for item in meta:
        if (raw := getattr(item, "pattern", None)) is not None:
            pattern = _PATTERNS.get(raw, "scope path" if raw.startswith("^(org|project") else raw)
        elif isinstance(item, Gt) and item.gt == 0:
            bound = "positive integer"
        elif isinstance(item, Ge):
            low = item.ge
        elif isinstance(item, Le):
            high = item.le
    if low is not None and high is not None:
        bound = f"integer {low} to {high}"
    return pattern, bound


def render_type(field: FieldInfo, links: Callable[[str], str]) -> str:
    return _render(field.annotation, list(field.metadata), links)


def _render(tp: Any, meta: list[Any], links: Callable[[str], str]) -> str:  # noqa: ANN401, PLR0911
    origin = get_origin(tp)
    if origin is Annotated:
        inner, *extra = get_args(tp)
        meta = [
            *meta,
            *(e for e in extra if not isinstance(e, FieldInfo)),
            *(m for e in extra if isinstance(e, FieldInfo) for m in e.metadata),
        ]
        return _render(inner, meta, links)
    if origin in (Union, types.UnionType):
        parts = [a for a in get_args(tp) if a is not type(None)]
        text = " or ".join(_render(a, meta, links) for a in parts)
        return text + (" (optional)" if len(parts) < len(get_args(tp)) else "")
    if origin is Literal:
        return " or ".join(f"`{a}`" for a in get_args(tp))
    if origin is list:
        return f"list of {_render(get_args(tp)[0], [], links)}"
    if origin is dict:
        key, value = get_args(tp)
        return f"map of {_render(key, [], links)} to {_render(value, [], links)}"
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        return links(tp.__name__)
    if tp is Money:
        return 'money string such as `"50 USD"`'
    if tp is bool:
        return "boolean"
    if tp is str:
        pattern, _ = _constraints(meta)
        if any(getattr(m, "func", None) is check_ref for m in meta):
            return "secret reference (`env:NAME` or `file:/path`)"
        if any(getattr(m, "func", None) is schema._decimal_text for m in meta):
            return "quoted decimal"
        return pattern or "string"
    if tp is int:
        _, bound = _constraints(meta)
        return bound or "integer"
    return str(tp)


def render_default(field: FieldInfo) -> str:
    if field.is_required():
        return "required"
    value = field.get_default(call_default_factory=True)
    text = f"`{value}`"
    match value:
        case BaseModel():
            text = "all unset" if not value.model_dump(exclude_none=True) else "see below"
        case None:
            text = "unset"
        case bool():
            text = f"`{str(value).lower()}`"
        case dict():
            text = "`{}`"
        case list():
            text = "`[" + ", ".join(f'"{v}"' for v in value) + "]`"
        case str():
            text = f'`"{value}"`'
    return text


def _model_types(tp: Any) -> list[type[BaseModel]]:  # noqa: ANN401
    found: list[type[BaseModel]] = []
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        return [tp]
    for arg in get_args(tp):
        found.extend(_model_types(arg))
    return found


def _collect() -> list[tuple[type[BaseModel], list[str]]]:
    """Every model reachable from Config, in first-seen order, with where it is used."""
    order: list[type[BaseModel]] = []
    paths: dict[type[BaseModel], list[str]] = {}
    queue: list[tuple[type[BaseModel], str]] = [(schema.Config, "")]
    while queue:
        model, path = queue.pop(0)
        if model not in paths:
            order.append(model)
            paths[model] = []
        if path and path not in paths[model]:
            paths[model].append(path)
        for name, field in model.model_fields.items():
            suffix = _suffix(field.annotation)
            for child in _model_types(field.annotation):
                queue.append((child, f"{path}.{name}".lstrip(".") + suffix))
    return [(m, paths[m]) for m in order]


def _suffix(tp: Any) -> str:  # noqa: ANN401
    inner = [tp]
    if get_origin(tp) in (Union, types.UnionType):
        inner = [a for a in get_args(tp) if a is not type(None)]
    if inner and get_origin(inner[0]) is list:
        return "[]"
    if inner and get_origin(inner[0]) is dict:
        return ".<name>"
    return ""


def generate() -> str:
    models = _collect()
    names = {m.__name__ for m, _ in models}

    def link(name: str) -> str:
        return f"[{name}](#{name.lower()})" if name in names else name

    lines = [
        "# Configuration reference",
        "",
        "<!-- Generated by scripts/gen_config_docs.py from spendgate.config.schema. "
        "Do not edit by hand. -->",
        "",
        "Every key of `spendgate.yaml`. Unknown keys are refused, so a typo fails at start. "
        'Money is a quoted string such as `"50 EUR"`, never a number. Secrets are never '
        "written here: use `env:NAME` or `file:/absolute/path`. Check a file without starting "
        "anything: `spendgate check-config --config spendgate.yaml`. A complete example is "
        "[`examples/config.yaml`](../examples/config.yaml).",
        "",
        "Durations carry their unit in the key name (`*_seconds`). The top-level sections are "
        "in the [Config](#config) table; each linked type has its own table below.",
        "",
    ]
    missing: list[str] = []
    for model, where in models:
        lines += [f"## {model.__name__}", ""]
        if where:
            lines += ["Used at " + ", ".join(f"`{w}`" for w in where) + ".", ""]
        lines += ["| Key | Type | Default | Description |", "|---|---|---|---|"]
        for name, field in model.model_fields.items():
            text = D.get(f"{model.__name__}.{name}")
            if text is None:
                missing.append(f"{model.__name__}.{name}")
                continue
            lines.append(
                f"| `{name}` | {render_type(field, link)} | {render_default(field)} | {text} |"
            )
        lines.append("")
    if missing:
        raise SystemExit(
            "undocumented config keys, add them to D in gen_config_docs.py: " + ", ".join(missing)
        )
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str]) -> int:
    text = generate()
    if "--check" in argv:
        if OUTPUT.read_text() != text:
            print(f"{OUTPUT} is stale: run scripts/gen_config_docs.py", file=sys.stderr)
            return 1
        return 0
    OUTPUT.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
