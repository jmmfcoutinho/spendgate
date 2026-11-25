"""Responses as data: error bodies in the OpenAI or Anthropic shape, and the gateway headers."""

import json
from collections.abc import Mapping

from spendgate.core.errors import ERROR_SPECS, ErrorCode, Refusal, Shape, error_type
from spendgate.core.money import Money
from spendgate.pipeline.model import Reply
from spendgate.providers.port import UpstreamResponse

REQUEST_ID_HEADER = "X-Spendgate-Request-Id"
COST_HEADER = "X-Spendgate-Cost"
REMAINING_HEADER = "X-Spendgate-Budget-Remaining"
SCOPE_HEADER = "X-Spendgate-Budget-Scope"
SHOULD_RETRY = "x-should-retry"
# Provider headers that survive: nothing else is passed on (protocol 5, last paragraph).
_PASS = frozenset({"content-type", "cache-control"})
_RETRYABLE = frozenset(
    {
        ErrorCode.RATE_LIMITED,
        ErrorCode.UPSTREAM_THROTTLED,
        ErrorCode.LEDGER_UNAVAILABLE,
        ErrorCode.BUDGET_PERIOD_EXHAUSTED,
    }
)


def gateway_headers(
    request_id: str,
    charged: Money | None = None,
    remaining: Money | None = None,
    scope: str | None = None,
) -> dict[str, str]:
    headers = {REQUEST_ID_HEADER: request_id}
    if charged is not None:
        headers[COST_HEADER] = charged.format()
    if remaining is not None and scope is not None:
        headers[REMAINING_HEADER] = remaining.format()
        headers[SCOPE_HEADER] = scope
    return headers


def error_body(refusal: Refusal, shape: Shape, request_id: str, status: int) -> bytes:
    """The wire body; `message` carries no secret or prompt text by construction of the callers."""
    inner: dict[str, object] = {
        "type": error_type(refusal.code, shape, status),
        "code": refusal.code.value,
        "message": refusal.message,
    }
    if shape == "openai" or refusal.param is not None:
        inner["param"] = refusal.param
    details = dict(refusal.details)
    if refusal.retry_after_seconds is not None:
        details["retry_after_seconds"] = refusal.retry_after_seconds
    if details:
        inner["details"] = details
    outer: dict[str, object] = (
        {"error": inner}
        if shape == "openai"
        else {"type": "error", "error": inner, "request_id": request_id}
    )
    return json.dumps(outer, separators=(",", ":")).encode()


def error_reply(  # noqa: PLR0913 - one response's independent facts
    refusal: Refusal,
    shape: Shape,
    request_id: str,
    *,
    status: int | None = None,
    charged: Money | None = None,
    remaining: Money | None = None,
    scope: str | None = None,
    should_retry: bool | None = None,
) -> Reply:
    """`should_retry` None: retryable codes allow retry, every other code sets `x-should-retry`."""
    code = status or ERROR_SPECS[refusal.code].http_status
    headers = gateway_headers(request_id, charged, remaining, scope)
    headers["Content-Type"] = "application/json"
    retry = refusal.code in _RETRYABLE if should_retry is None else should_retry
    if not retry:
        headers[SHOULD_RETRY] = "false"
    if refusal.retry_after_seconds is not None:
        headers["Retry-After"] = str(refusal.retry_after_seconds)
    return Reply(code, headers, error_body(refusal, shape, request_id, code))


def stream_headers(
    request_id: str, remaining: Money | None = None, scope: str | None = None
) -> dict[str, str]:
    """Headers of a live stream (protocol 2.8): sent before generation, so no cost here; the
    budget room is the pre-call lower bound (after the hold)."""
    headers = {"Content-Type": "text/event-stream", "Cache-Control": "no-store"}
    return headers | gateway_headers(request_id, None, remaining, scope)


def success_reply(
    response: UpstreamResponse,
    request_id: str,
    charged: Money | None,
    remaining: Money | None = None,
    scope: str | None = None,
) -> Reply:
    headers = _passed(response.headers)
    headers.update(gateway_headers(request_id, charged, remaining, scope))
    return Reply(response.status, headers, response.body)


def _passed(upstream: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in upstream.items() if k.lower() in _PASS}


def models_reply(names: list[str], shape: Shape, request_id: str, single: bool) -> Reply:
    """The OpenAI model list (or one model), or the Anthropic list when the client speaks it."""
    if shape == "anthropic":
        items: list[dict[str, object]] = [
            {"type": "model", "id": n, "display_name": n, "created_at": "1970-01-01T00:00:00Z"}
            for n in names
        ]
        anthropic: dict[str, object] = (
            items[0]
            if single
            else {
                "data": items,
                "has_more": False,
                "first_id": names[0] if names else None,
                "last_id": names[-1] if names else None,
            }
        )
        body = anthropic
    else:
        models: list[dict[str, object]] = [
            {"id": n, "object": "model", "created": 0, "owned_by": "spendgate"} for n in names
        ]
        body = models[0] if single else {"object": "list", "data": models}
    headers = {"Content-Type": "application/json", REQUEST_ID_HEADER: request_id}
    return Reply(200, headers, json.dumps(body, separators=(",", ":")).encode())


def budget_details(refusal: Refusal, currency: str) -> tuple[Money | None, str | None]:
    """Budget headers for budget refusals: scope and remaining come from the ledger's details."""
    scope = refusal.details.get("scope")
    left = refusal.details.get("remaining")
    if not isinstance(scope, str) or not isinstance(left, str):
        return None, None
    amount, _, code = left.partition(" ")
    try:
        return Money.from_amount(amount, code or currency), scope
    except ValueError:
        return None, None
