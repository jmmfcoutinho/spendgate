"""The typed shape of spendgate.yaml. Every model forbids unknown keys so a typo fails to load.

Money is a quoted string ("50 USD") parsed to `Money`, never a float. Durations keep the unit in
the key name (`*_seconds`), as the docs do; `Settings` exposes milliseconds. Defaults exist only
where the docs give one.
"""

import re
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from spendgate.config.secrets import check_ref
from spendgate.core.money import Money, MoneyError, parse_amount

# Must match providers.anthropic: older SDKs send 2023-06-01; 2026-10-21 is the current version.
ANTHROPIC_VERSIONS = ("2023-06-01", "2026-10-21")
DEFAULT_MAX_BODY_BYTES = 26_214_400  # 25 MiB: protocol.md "25 MB" body and 25 MB transcription file
DEFAULT_MAX_JSON_BODY_BYTES = 4_194_304  # 4 MiB: JSON parses into ~25x its size (SR-01)
_SCOPE = r"(org|project|service|user):[A-Za-z0-9._-]+"
_SCOPE_PATH = rf"^{_SCOPE}(/{_SCOPE})*$"


def _money(value: object) -> Money:
    # No echo of the input; a float is rejected because 0.1 is not 0.1.
    if not isinstance(value, str):
        raise ValueError("must be a quoted amount such as '50 USD'")
    try:
        return parse_amount(value)
    except MoneyError:
        raise ValueError("must be a quoted amount such as '50 USD'") from None


def _decimal_text(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(\.[0-9]+)?", value):
        raise ValueError("must be a quoted decimal string such as '0.92'")
    return value


Amount = Annotated[Money, BeforeValidator(_money)]
SecretRef = Annotated[str, AfterValidator(check_ref)]
PosInt = Annotated[int, Field(strict=True, gt=0)]
Currency = Annotated[str, Field(strict=True, pattern=r"^[A-Z]{3}$")]  # ISO 4217 code
ScopePath = Annotated[str, Field(strict=True, pattern=_SCOPE_PATH)]
Listen = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9.\-:\[\]]+:[0-9]{1,5}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, arbitrary_types_allowed=True
    )


class AdminServer(_Strict):
    listen: Listen


class Server(_Strict):
    listen: Listen
    admin: AdminServer | None = None
    max_body_bytes: PosInt = DEFAULT_MAX_BODY_BYTES
    max_json_body_bytes: PosInt = DEFAULT_MAX_JSON_BODY_BYTES
    body_read_timeout_seconds: PosInt = 30  # whole-body deadline: a stalled upload frees its slot
    max_request_seconds: PosInt = 900  # one call or stream, end to end (SR-13)
    max_concurrent_requests: PosInt = 128  # not in the docs; the pipeline needs a bound (5.1)
    # Above common load balancer idle timeouts (60 s), so the balancer closes an idle connection
    # first and never reuses one this side already dropped (ADR-0015).
    keep_alive_seconds: PosInt = 75
    # Drain deadline on SIGTERM; must end before the orchestrator's kill (ADR-0015).
    graceful_shutdown_seconds: PosInt = 30
    key_pepper: SecretRef  # HMAC key for stored key secrets (threat model C-01)


class SqliteLedger(_Strict):
    kind: Literal["sqlite"]
    path: Annotated[str, Field(strict=True, min_length=1)]


class PostgresLedger(_Strict):
    kind: Literal["postgres"]
    dsn: SecretRef  # a DSN carries the password, so it is referenced like a secret


class Ledger(_Strict):
    backend: Annotated[SqliteLedger | PostgresLedger, Field(discriminator="kind")]
    currency: Currency
    lease_seconds: PosInt = 60
    heartbeat_seconds: PosInt | None = None  # default lease/3 (ledger-state-machine.md)
    sweep_seconds: PosInt = 30  # how often the server charges and releases expired holds
    retention_days: PosInt = 90

    @model_validator(mode="after")
    def _heartbeat_inside_lease(self) -> Self:
        if self.heartbeat_seconds is not None and self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError("heartbeat_seconds must be shorter than lease_seconds")
        return self


class Fx(_Strict):
    rates: dict[
        Currency, Annotated[str, AfterValidator(_decimal_text)]
    ]  # 1 unit of key = rate ledger units
    as_of: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    source: Annotated[str, Field(min_length=1)]


class CatalogPaths(_Strict):
    paths: list[Annotated[str, Field(strict=True, min_length=1)]] = Field(default_factory=list)


ProviderSlug = Literal[
    "openai", "azure_openai", "anthropic", "bedrock", "google", "azure_speech", "elevenlabs"
]
AuthName = Literal["api_key", "bearer_token", "sigv4"]


class Provider(_Strict):
    """Per-provider connection defaults that a deployment may override."""

    base_url: Annotated[str, Field(strict=True, min_length=1)] | None = None
    auth: AuthName = "api_key"
    credential: SecretRef | None = None  # none for sigv4 (the standard AWS chain)
    region: str | None = None
    api_version: str | None = None


class DeploymentSpec(_Strict):
    provider: ProviderSlug
    model: Annotated[str, Field(strict=True, min_length=1)] | None = None  # default: catalog model
    base_url: Annotated[str, Field(strict=True, min_length=1)] | None = None
    auth: AuthName | None = None
    credential: SecretRef | None = None
    region: str | None = None
    api_version: str | None = None


class ModelRoute(_Strict):
    """An alias: one catalog id served by ordered deployments (fallback only within the id)."""

    catalog_id: Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_]*/.+$")]
    deployments: Annotated[list[DeploymentSpec], Field(min_length=1)]


# Calls in flight per key when neither the key nor `defaults.limits` says otherwise: it bounds
# the overrun at a cap crossing (how-money-is-protected.md) and matches the Compose reference.
DEFAULT_CONCURRENCY = 16
# New sessions per key and hour once a key has `session_defaults`, so minting ids cannot dodge
# the session limits at an unbounded rate (TH-09). `null` is the explicit opt-out.
DEFAULT_NEW_SESSIONS_PER_HOUR = 60


class Limits(_Strict):
    rpm: PosInt | None = None
    tpm: PosInt | None = None
    concurrency: PosInt | None = None


class DefaultLimits(Limits):
    """`defaults.limits`: like a key's limits, but concurrency is bounded unless set to null."""

    concurrency: PosInt | None = DEFAULT_CONCURRENCY


class Defaults(_Strict):
    limits: DefaultLimits = Field(default_factory=DefaultLimits)
    max_output_tokens: PosInt | None = None
    max_embedding_items: PosInt = 256
    strict_overrun: bool = True


class FailOpen(_Strict):
    """Explicit, named opt-outs. Each enabled one is logged as a warning at start."""

    allow_remote_media: bool = False  # SSRF: provider-side fetch of URLs in bodies


class Routing(_Strict):
    max_attempts: PosInt = 2
    cooldown_seconds: PosInt = 5  # LiteLLM defaults, as `core.health.HealthConfig`
    failure_threshold_pct: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
    min_requests: PosInt = 5
    window_seconds: PosInt = 60
    max_cooldown_seconds: PosInt = 300
    connect_timeout_seconds: PosInt = 10

    @model_validator(mode="after")
    def _cooldown_order(self) -> Self:
        if self.cooldown_seconds > self.max_cooldown_seconds:
            raise ValueError("cooldown_seconds must not exceed max_cooldown_seconds")
        return self


class Anthropic(_Strict):
    allowed_versions: list[str] = Field(default_factory=lambda: list(ANTHROPIC_VERSIONS))
    allowed_betas: list[str] = Field(default_factory=list)


class BootstrapBudget(_Strict):
    id: Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
    scope: ScopePath
    period: Literal["none", "day", "month"]
    cap: Amount
    soft_pct: Annotated[int, Field(strict=True, ge=1, le=99)] = 80
    model_filter: str | None = None
    strict_overrun: bool | None = None  # None = defaults.strict_overrun


class SessionDefaultsSpec(_Strict):
    budget: Amount | None = None
    max_steps: PosInt | None = None
    max_seconds: PosInt | None = None
    idle_seconds: PosInt | None = None
    max_new_sessions_per_hour: PosInt | None = DEFAULT_NEW_SESSIONS_PER_HOUR


class BootstrapKey(_Strict):
    name: Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
    scope: ScopePath | None = None  # data keys
    admin_scopes: list[Literal["usage:read", "keys:write", "budgets:write"]] | None = None
    models: list[str] | None = None  # allow list: aliases or catalog ids; None = every routed model
    limits: Limits = Field(default_factory=Limits)
    max_output_tokens: PosInt | None = None
    session_defaults: SessionDefaultsSpec | None = None
    session_required: bool = False  # every call must send X-Spendgate-Session

    @model_validator(mode="after")
    def _data_xor_admin(self) -> Self:
        if (self.scope is None) == (self.admin_scopes is None):
            raise ValueError("give exactly one of scope (data key) or admin_scopes (admin key)")
        if self.session_required and self.session_defaults is None:
            raise ValueError("session_required needs session_defaults")
        return self


class Bootstrap(_Strict):
    """Create-only and idempotent: existing rows are never changed by a restart."""

    scopes: list[ScopePath] = Field(default_factory=list)
    budgets: list[BootstrapBudget] = Field(default_factory=list)
    keys: list[BootstrapKey] = Field(default_factory=list)


class Config(_Strict):
    server: Server
    ledger: Ledger
    fx: Fx | None = None
    catalog: CatalogPaths = Field(default_factory=CatalogPaths)
    providers: dict[ProviderSlug, Provider] = Field(default_factory=dict)
    models: dict[
        Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")], ModelRoute
    ]
    defaults: Defaults = Field(default_factory=Defaults)
    fail_open: FailOpen = Field(default_factory=FailOpen)
    bootstrap: Bootstrap = Field(default_factory=Bootstrap)
    anthropic: Anthropic = Field(default_factory=Anthropic)
    routing: Routing = Field(default_factory=Routing)
