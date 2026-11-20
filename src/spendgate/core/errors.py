"""The protocol's error codes with HTTP status and wire `type`, as data (protocol.md section 4)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

type Shape = Literal["openai", "anthropic"]
type DetailValue = str | int


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BUDGET_PERIOD_EXHAUSTED = "budget_period_exhausted"
    SESSION_LIMIT_REACHED = "session_limit_reached"
    FORBIDDEN = "forbidden"
    MODEL_NOT_ALLOWED = "model_not_allowed"
    MODEL_NOT_PRICED = "model_not_priced"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    REQUEST_TOO_LARGE = "request_too_large"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_THROTTLED = "upstream_throttled"
    INTERNAL_ERROR = "internal_error"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    KILL_SWITCH = "kill_switch"
    LEDGER_UNAVAILABLE = "ledger_unavailable"


class LimitName(StrEnum):
    """`details.limit` values."""

    BUDGET = "budget"
    STEPS = "steps"
    TIME = "time"
    NEW_SESSIONS = "new_sessions"
    RPM = "rpm"
    TPM = "tpm"
    CONCURRENCY = "concurrency"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    http_status: int
    openai_type: str
    anthropic_type: str
    retry_after: bool  # whether the response normally carries Retry-After


def _spec(
    status: int, openai: str, anthropic: str | None = None, *, retry: bool = False
) -> ErrorSpec:
    return ErrorSpec(status, openai, anthropic or openai, retry)


ERROR_SPECS: Mapping[ErrorCode, ErrorSpec] = MappingProxyType(
    {
        ErrorCode.INVALID_REQUEST: _spec(400, "invalid_request_error"),
        ErrorCode.UNAUTHORIZED: _spec(401, "authentication_error"),
        ErrorCode.BUDGET_EXHAUSTED: _spec(402, "budget_error", "billing_error"),
        ErrorCode.BUDGET_PERIOD_EXHAUSTED: _spec(402, "budget_error", "billing_error", retry=True),
        ErrorCode.SESSION_LIMIT_REACHED: _spec(402, "budget_error", "billing_error"),
        ErrorCode.FORBIDDEN: _spec(403, "permission_error"),
        ErrorCode.MODEL_NOT_ALLOWED: _spec(403, "permission_error"),
        ErrorCode.MODEL_NOT_PRICED: _spec(404, "invalid_request_error", "not_found_error"),
        ErrorCode.NOT_FOUND: _spec(404, "invalid_request_error", "not_found_error"),
        ErrorCode.CONFLICT: _spec(409, "invalid_request_error", "conflict_error"),
        ErrorCode.REQUEST_TOO_LARGE: _spec(413, "invalid_request_error", "request_too_large"),
        ErrorCode.RATE_LIMITED: _spec(429, "rate_limit_error", retry=True),
        ErrorCode.UPSTREAM_THROTTLED: _spec(429, "rate_limit_error", retry=True),
        ErrorCode.INTERNAL_ERROR: _spec(500, "server_error", "api_error"),
        # 502 default; 504 (timeout) and 503 (every deployment circuit-open) below.
        ErrorCode.UPSTREAM_UNAVAILABLE: _spec(502, "upstream_error", "api_error"),
        ErrorCode.KILL_SWITCH: _spec(503, "server_error", "api_error"),
        ErrorCode.LEDGER_UNAVAILABLE: _spec(503, "server_error", "api_error", retry=True),
    }
)

UPSTREAM_TIMEOUT_STATUS = 504
UPSTREAM_CIRCUIT_OPEN_STATUS = 503


def error_type(code: ErrorCode, shape: Shape, status: int | None = None) -> str:
    """Wire `type` for `code` in the OpenAI or Anthropic body shape."""
    if code is ErrorCode.UPSTREAM_UNAVAILABLE and status == UPSTREAM_TIMEOUT_STATUS:
        return "timeout_error" if shape == "anthropic" else "upstream_error"
    spec = ERROR_SPECS[code]
    return spec.anthropic_type if shape == "anthropic" else spec.openai_type


@dataclass(frozen=True, slots=True)
class Refusal:
    """A refused call as data: the pipeline turns it into a response, nothing is raised."""

    code: ErrorCode
    message: str
    param: str | None = None
    details: Mapping[str, DetailValue] = field(default_factory=dict)
    retry_after_seconds: int | None = None

    @property
    def http_status(self) -> int:
        return ERROR_SPECS[self.code].http_status
