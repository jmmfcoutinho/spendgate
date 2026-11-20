"""Error codes as data: HTTP status and wire type per code (protocol.md section 4.2)."""

import pytest

from spendgate.core.errors import (
    ERROR_SPECS,
    ErrorCode,
    LimitName,
    Refusal,
    error_type,
)

# (code, status, openai type, anthropic type, retry-after)
PROTOCOL_TABLE = [
    ("invalid_request", 400, "invalid_request_error", "invalid_request_error", False),
    ("unauthorized", 401, "authentication_error", "authentication_error", False),
    ("budget_exhausted", 402, "budget_error", "billing_error", False),
    ("budget_period_exhausted", 402, "budget_error", "billing_error", True),
    ("session_limit_reached", 402, "budget_error", "billing_error", False),
    ("forbidden", 403, "permission_error", "permission_error", False),
    ("model_not_allowed", 403, "permission_error", "permission_error", False),
    ("model_not_priced", 404, "invalid_request_error", "not_found_error", False),
    ("not_found", 404, "invalid_request_error", "not_found_error", False),
    ("conflict", 409, "invalid_request_error", "conflict_error", False),
    ("request_too_large", 413, "invalid_request_error", "request_too_large", False),
    ("rate_limited", 429, "rate_limit_error", "rate_limit_error", True),
    ("upstream_throttled", 429, "rate_limit_error", "rate_limit_error", True),
    ("internal_error", 500, "server_error", "api_error", False),
    ("upstream_unavailable", 502, "upstream_error", "api_error", False),
    ("kill_switch", 503, "server_error", "api_error", False),
    ("ledger_unavailable", 503, "server_error", "api_error", True),
]


def test_the_error_table_covers_every_code_exactly_once() -> None:
    assert set(ERROR_SPECS) == set(ErrorCode)
    assert [row[0] for row in PROTOCOL_TABLE] == [c.value for c in ErrorCode]


@pytest.mark.parametrize(("code", "status", "openai", "anthropic", "retry"), PROTOCOL_TABLE)
def test_error_code_matches_the_protocol_table(
    code: str, status: int, openai: str, anthropic: str, retry: bool
) -> None:
    spec = ERROR_SPECS[ErrorCode(code)]
    assert (spec.http_status, spec.openai_type, spec.anthropic_type, spec.retry_after) == (
        status,
        openai,
        anthropic,
        retry,
    )
    assert error_type(ErrorCode(code), "openai") == openai
    assert error_type(ErrorCode(code), "anthropic") == anthropic


def test_upstream_timeout_has_its_own_anthropic_type() -> None:
    code = ErrorCode.UPSTREAM_UNAVAILABLE
    assert error_type(code, "anthropic", 504) == "timeout_error"
    assert error_type(code, "openai", 504) == "upstream_error"
    assert error_type(code, "anthropic", 502) == "api_error"
    assert error_type(ErrorCode.KILL_SWITCH, "anthropic", 504) == "api_error"


def test_limit_names_are_the_protocol_details_limit_values() -> None:
    assert {x.value for x in LimitName} == {
        "budget", "steps", "time", "new_sessions", "rpm", "tpm", "concurrency",
    }  # fmt: skip


def test_refusal_exposes_its_http_status() -> None:
    refusal = Refusal(ErrorCode.SESSION_LIMIT_REACHED, "x", None, {"limit": "steps"})
    assert refusal.http_status == 402
    assert refusal.details == {"limit": "steps"}
    assert Refusal(ErrorCode.INVALID_REQUEST, "x", "model").param == "model"
