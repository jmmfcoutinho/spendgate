"""Request bodies of the admin API as strict pydantic models (one per OpenAPI request schema).

Unknown fields are refused (fail closed), money is a decimal string plus currency and never a
float, and times are RFC 3339 strings turned into epoch milliseconds here, once.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from spendgate.config.schema import DEFAULT_NEW_SESSIONS_PER_HOUR

MAX_REASON = 500
_AMOUNT = r"^[0-9]+(\.[0-9]{1,9})?$"
_SCOPE_PATH = (
    r"^([a-z]+:[A-Za-z0-9._-]{1,64}/){0,4}"
    r"(session:[A-Za-z0-9._:#-]{1,300}|[a-z]+:[A-Za-z0-9._-]{1,64})$"
)


def rfc3339_ms(value: object) -> int:
    """`2026-11-17T09:00:00Z` to epoch ms; a time without an offset is refused."""
    if not isinstance(value, str):
        raise ValueError("must be an RFC 3339 time")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("must be an RFC 3339 time such as 2026-11-17T09:00:00Z") from None
    if moment.tzinfo is None:
        raise ValueError("must carry a UTC offset")
    try:
        return round(moment.astimezone(UTC).timestamp() * 1000)
    except OverflowError:  # year 1 with a positive offset has no UTC time (SR-07)
        raise ValueError("must be a time within the supported range") from None


Time = Annotated[int, BeforeValidator(rfc3339_ms)]
Reason = Annotated[str, Field(strict=True, min_length=1, max_length=MAX_REASON)]
PosInt = Annotated[int, Field(strict=True, ge=1)]
# `[]` would read as "no models" but is stored as "every model" (SR-09): refuse it. Absent or null
# means every model; to deny a key everything, revoke it.
ModelList = Annotated[list[str], Field(min_length=1)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class MoneyIn(_In):
    amount: Annotated[str, Field(pattern=_AMOUNT)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class ScopeCreateIn(_In):
    path: Annotated[str, Field(pattern=_SCOPE_PATH)]
    display_name: Annotated[str, Field(max_length=120)] | None = None


class ScopeUpdateIn(_In):
    display_name: Annotated[str, Field(max_length=120)] | None = None


class KeyLimitsIn(_In):
    rpm: PosInt | None = None
    tpm: PosInt | None = None
    max_concurrent: PosInt | None = None
    max_output_tokens: PosInt | None = None
    per_call_ceiling_nano: Annotated[int, Field(strict=True, ge=0)] | None = None


class SessionDefaultsIn(_In):
    budget_nano: Annotated[int, Field(strict=True, ge=0)] | None = None
    max_steps: PosInt | None = None
    max_seconds: PosInt | None = None
    idle_seconds: PosInt | None = None
    # The same default as the config's `session_defaults`; an explicit null opts out.
    max_new_sessions_per_hour: PosInt | None = DEFAULT_NEW_SESSIONS_PER_HOUR


class KeyCreateIn(_In):
    scope_id: str
    name: Annotated[str, Field(max_length=120)] | None = None
    allowed_models: ModelList | None = None
    limits: KeyLimitsIn | None = None
    session_defaults: SessionDefaultsIn | None = None
    session_required: bool = False  # needs session_defaults
    expires_at: Time | None = None


class KeyUpdateIn(_In):
    """Absent keeps the stored value; `expires_at: null` clears it (`model_fields_set` tells)."""

    name: Annotated[str, Field(max_length=120)] | None = None
    allowed_models: ModelList | None = None
    limits: KeyLimitsIn | None = None
    session_defaults: SessionDefaultsIn | None = None
    session_required: bool = False  # absent keeps the stored value
    expires_at: Time | None = None


class RotateIn(_In):
    grace_seconds: Annotated[int, Field(strict=True, ge=0, le=604_800)] = 3_600


class BudgetCreateIn(_In):
    period: Literal["none", "day", "month"] = "none"
    cap: MoneyIn
    soft_pct: Annotated[int, Field(strict=True, ge=1, le=99)] = 80
    model_filter: str | None = None
    strict_overrun: bool | None = None  # absent: the configured `defaults.strict_overrun`
    note: Annotated[str, Field(max_length=MAX_REASON)] | None = None


class BudgetUpdateIn(_In):
    cap: MoneyIn | None = None
    soft_pct: Annotated[int, Field(strict=True, ge=1, le=99)] | None = None
    strict_overrun: bool | None = None
    note: Annotated[str, Field(max_length=MAX_REASON)] | None = None


class GrantCreateIn(_In):
    amount: MoneyIn
    valid_from: Time | None = None
    valid_until: Time
    reason: Reason


class AckIn(_In):
    reason: Reason


class KillSwitchCreateIn(_In):
    target_kind: Literal["global", "scope", "key", "provider", "model"]
    target_ref: str | None = None
    reason: Reason
