"""Plain data for virtual keys and admin keys; no I/O, no secrets except `NewKey`."""

from dataclasses import dataclass, field
from enum import StrEnum

from spendgate.core.policy import KeyPolicy
from spendgate.core.sessions import SessionDefaults

MAX_NAME = 120


class KeyKind(StrEnum):
    API = "api"  # data plane, `sg-<prefix>-<secret>`
    ADMIN = "admin"  # control plane, `sga-<prefix>-<secret>`


class AdminScope(StrEnum):
    """What an admin key may do. Independent: `keys:write` does not imply `usage:read`."""

    USAGE_READ = "usage:read"
    KEYS_WRITE = "keys:write"
    BUDGETS_WRITE = "budgets:write"


@dataclass(frozen=True, slots=True)
class KeySpec:
    """Everything chosen when a key is made. Admin keys carry scopes and no policy."""

    scope_id: str
    name: str
    kind: KeyKind = KeyKind.API
    allowed_models: tuple[str, ...] | None = None  # None = every model; () is refused (SR-09)
    max_output_tokens: int | None = None
    per_call_ceiling_nano: int | None = None
    rpm: int | None = None
    tpm: int | None = None
    max_concurrent: int | None = None
    session_defaults: SessionDefaults | None = None
    admin_scopes: frozenset[AdminScope] = frozenset()
    expires_at: int | None = None

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > MAX_NAME:
            raise ValueError("a key name is 1 to 120 characters")
        if (self.kind is KeyKind.ADMIN) != bool(self.admin_scopes):
            raise ValueError("an admin key needs at least one admin scope; an api key has none")
        numbers = (
            self.max_output_tokens,
            self.per_call_ceiling_nano,
            self.rpm,
            self.tpm,
            self.max_concurrent,
        )
        if any(n is not None and n <= 0 for n in numbers):
            raise ValueError("key limits must be positive")


@dataclass(frozen=True, slots=True)
class KeyRecord:
    """A key as stored, without its hash. Safe to list and log."""

    id: str
    prefix: str
    kind: KeyKind
    name: str
    created_at: int
    policy: KeyPolicy
    admin_scopes: frozenset[AdminScope] = frozenset()
    expires_at: int | None = None  # valid while now < expires_at; rotation sets it on the old key
    revoked_at: int | None = None
    replaces_key_id: str | None = None

    @property
    def scope_id(self) -> str:
        return self.policy.scope_id


@dataclass(frozen=True, slots=True)
class StoredKey:
    """What lookup by prefix returns: the record plus the hex HMAC to compare against."""

    record: KeyRecord
    secret_hash: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class NewKey:
    """The only place a secret exists. `secret` is the full `sg-...` string, shown once."""

    record: KeyRecord
    secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Principal:
    """Who a verified key is: what the pipeline needs, nothing secret."""

    key_id: str
    kind: KeyKind
    scope_id: str
    policy: KeyPolicy
    admin_scopes: frozenset[AdminScope] = frozenset()
