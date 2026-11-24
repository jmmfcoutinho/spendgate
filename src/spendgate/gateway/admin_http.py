"""The admin API's plumbing: auth, error mapping, body and query parsing, cursors, money, time.

Handlers raise `ApiError` (or let a ledger error through) and return plain `Response`s; the
`guarded` wrapper turns every failure into the OpenAI error shape of protocol.md section 4
and never puts a secret or exception text in a message.
"""

import base64
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from spendgate import package_version
from spendgate.config import Settings
from spendgate.core.errors import ERROR_SPECS, DetailValue, ErrorCode, Refusal
from spendgate.core.money import Money
from spendgate.core.policy import KeyPolicy
from spendgate.gateway.admin_models import MoneyIn
from spendgate.gateway.logs import LOGGER_NAME, log_internal
from spendgate.gateway.metrics import Metrics
from spendgate.gateway.observability import ReadinessChecks
from spendgate.ledger.admin_port import AdminLedger
from spendgate.ledger.key_types import AdminScope, KeyKind, Principal
from spendgate.ledger.keys import KeyService
from spendgate.ledger.port import ConflictError, LedgerError, LedgerUnavailable, NotFoundError
from spendgate.ledger.usage import UsageQueryError, UsageRangeError
from spendgate.pipeline.faults import InternalEvent, fault_of
from spendgate.pipeline.model import Clock
from spendgate.pipeline.replies import REQUEST_ID_HEADER, error_body

MAX_BODY = 1_048_576  # admin bodies are small JSON documents
DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 500
_BEARER = "bearer "
_RELATIVE = re.compile(r"(\d{1,6})([smhdw])")
_UNIT_MS = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
_LEDGER_RETRY_SECONDS = 5


@dataclass(frozen=True, slots=True)
class AdminContext:
    """Everything the admin routes need; built once by the gateway or the CLI."""

    ledger: AdminLedger
    keys: KeyService
    settings: Settings
    now: Clock
    started_ms: int
    metrics: Metrics | None = None
    version: str = field(default_factory=package_version)
    # CLI `--local` only: requests need no admin key and are audited under this actor. The
    # caller already holds the ledger itself (file or DSN), which is a stronger credential.
    trusted_actor: str | None = None
    readiness: ReadinessChecks | None = None  # served in detail at /admin/v1/readiness (SR-06)

    @property
    def currency(self) -> str:
        return self.settings.ledger_currency


class ApiError(Exception):
    """A refusal the handler decided on; `param` names the offending field when there is one."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        param: str | None = None,
        details: Mapping[str, DetailValue] | None = None,
    ) -> None:
        super().__init__(message)
        self.refusal = Refusal(code, message, param, dict(details or {}))


@dataclass(frozen=True, slots=True)
class Call:
    """One authenticated admin request."""

    ctx: AdminContext
    request: Request
    principal: Principal
    now: int
    request_id: str

    @property
    def actor(self) -> str:
        return self.principal.key_id

    def path(self, name: str) -> str:
        return str(self.request.path_params[name])

    def query(self, name: str) -> str | None:
        return self.request.query_params.get(name)

    def int_query(self, name: str, default: int, low: int, high: int) -> int:
        raw = self.query(name)
        if raw is None:
            return default
        if not raw.isdecimal() or not low <= int(raw) <= high:
            raise ApiError(ErrorCode.INVALID_REQUEST, f"{name} must be {low}..{high}.", name)
        return int(raw)

    def limit(self) -> int:
        return self.int_query("limit", DEFAULT_LIMIT, 1, MAX_LIMIT)

    def time_query(self, name: str) -> int | None:
        raw = self.query(name)
        return None if raw is None else parse_time(raw, self.now, name)

    async def body[M: BaseModel](self, model: type[M], *, optional: bool = False) -> M:
        raw = await _read_body(self.request)
        if not raw.strip():
            if optional:
                return model()
            raise ApiError(ErrorCode.INVALID_REQUEST, "A JSON body is required.")
        try:
            return model.model_validate_json(raw)
        except ValidationError as exc:
            first = exc.errors()[0]
            param = ".".join(str(p) for p in first["loc"]) or None
            # `msg` describes the rule; the input value (which may be a secret) is never echoed.
            raise ApiError(
                ErrorCode.INVALID_REQUEST, f"Invalid body: {first['msg']}.", param
            ) from None


type Handler = Callable[[Call], Awaitable[Response]]


async def _read_body(request: Request) -> bytes:
    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise ApiError(ErrorCode.REQUEST_TOO_LARGE, "The request body is too large.")
        chunks.append(chunk)
    return b"".join(chunks)


# --- responses -------------------------------------------------------------------------------


def error_response(refusal: Refusal, request_id: str) -> Response:
    status = ERROR_SPECS[refusal.code].http_status
    headers = {REQUEST_ID_HEADER: request_id, "Content-Type": "application/json"}
    if refusal.retry_after_seconds is not None:
        headers["Retry-After"] = str(refusal.retry_after_seconds)
    return Response(error_body(refusal, "openai", request_id, status), status, headers)


def respond(data: object, status: int = 200) -> Response:
    return JSONResponse(data, status_code=status)


def no_content() -> Response:
    return Response(status_code=204)


def refusal_for(exc: Exception) -> Refusal | None:
    """Ledger and validation failures as refusals; None for anything unexpected (a 500)."""
    if isinstance(exc, UsageQueryError):  # UsageRangeError (a `since` past retention) is one
        ranged = isinstance(exc, UsageRangeError)
        message = "since is older than retention." if ranged else str(exc)
        return Refusal(ErrorCode.INVALID_REQUEST, message, "since" if ranged else "by")
    if isinstance(exc, LedgerUnavailable):
        return Refusal(
            ErrorCode.LEDGER_UNAVAILABLE,
            "The ledger is unavailable; try again.",
            retry_after_seconds=_LEDGER_RETRY_SECONDS,
        )
    if isinstance(exc, NotFoundError):
        return Refusal(ErrorCode.NOT_FOUND, "That object does not exist.")
    if isinstance(exc, ConflictError):
        return Refusal(ErrorCode.CONFLICT, str(exc))
    if isinstance(exc, ValueError):
        # Ledger and core validators raise ValueError with a rule, never with input values.
        return Refusal(ErrorCode.INVALID_REQUEST, str(exc))
    return None


async def authenticate(
    ctx: AdminContext, request: Request, scope: AdminScope
) -> Principal | Refusal:
    header = request.headers.get("authorization", "")
    raw = header[len(_BEARER) :].strip() if header.lower().startswith(_BEARER) else ""
    if not raw:
        return Refusal(ErrorCode.UNAUTHORIZED, "Invalid or missing admin key.")
    try:
        return await ctx.keys.verify_admin(raw, ctx.now(), scope)
    except LedgerError:
        return Refusal(
            ErrorCode.LEDGER_UNAVAILABLE,
            "The ledger is unavailable; try again.",
            retry_after_seconds=_LEDGER_RETRY_SECONDS,
        )


def _trusted(ctx: AdminContext) -> Principal:
    actor = ctx.trusted_actor or ""
    return Principal(actor, KeyKind.ADMIN, "", KeyPolicy(actor, ""), frozenset(AdminScope))


def guarded(
    ctx: AdminContext, scope: AdminScope, handler: Handler
) -> Callable[[Request], Awaitable[Response]]:
    """A Starlette view: authenticate with `scope`, run the handler, map every failure."""

    async def view(request: Request) -> Response:
        request_id = f"sg_adm_{uuid.uuid4().hex}"
        found = _trusted(ctx) if ctx.trusted_actor else await authenticate(ctx, request, scope)
        if isinstance(found, Refusal):
            return error_response(found, request_id)
        call = Call(ctx, request, found, ctx.now(), request_id)
        try:
            response = await handler(call)
        except ApiError as exc:
            return error_response(exc.refusal, request_id)
        except Exception as exc:
            refusal = refusal_for(exc)
            if refusal is None:  # a bug: leave a redacted trace on the JSON log (same logger)
                fault = fault_of("admin.handler", exc)
                log_internal(logging.getLogger(LOGGER_NAME), InternalEvent(request_id, fault))
                refusal = Refusal(ErrorCode.INTERNAL_ERROR, "Internal error.")
            return error_response(refusal, request_id)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    return view


# --- values ----------------------------------------------------------------------------------


def iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def iso_or_none(ms: int | None) -> str | None:
    return None if ms is None else iso(ms)


def money_json(nano: int, currency: str) -> dict[str, str]:
    """`{"amount": "12.500000000", "currency": "EUR"}`; negative only for derived `remaining`."""
    whole, frac = divmod(abs(nano), 10**9)
    sign = "-" if nano < 0 else ""
    return {"amount": f"{sign}{whole}.{frac:09d}", "currency": currency}


def nano_of(ctx: AdminContext, value: MoneyIn, param: str) -> int:
    if value.currency != ctx.currency:
        raise ApiError(
            ErrorCode.INVALID_REQUEST,
            f"Money must be in the ledger currency {ctx.currency}.",
            f"{param}.currency",
        )
    return Money.from_amount(value.amount, value.currency).nano


def parse_time(raw: str, now: int, param: str) -> int:
    """An RFC 3339 time or a relative duration (`90m`, `7d`) before now, as epoch ms."""
    relative = _RELATIVE.fullmatch(raw)
    if relative is not None:
        return now - int(relative[1]) * _UNIT_MS[relative[2]]
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        message = "Use an RFC 3339 time with an offset, or a duration like 7d."
        raise ApiError(ErrorCode.INVALID_REQUEST, message, param)
    try:
        return round(moment.astimezone(UTC).timestamp() * 1000)
    except OverflowError:  # year 1 with a positive offset has no UTC time (SR-07)
        raise ApiError(ErrorCode.INVALID_REQUEST, "That time is out of range.", param) from None


def encode_cursor(*parts: int | str) -> str:
    return base64.urlsafe_b64encode(json.dumps(parts).encode()).decode().rstrip("=")


def decode_cursor(raw: str | None, shape: tuple[type, ...]) -> tuple[int | str, ...] | None:
    """The values a cursor carries, or None when absent; a malformed one is `invalid_request`."""
    if raw is None:
        return None
    try:
        loaded = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        ok = isinstance(loaded, list) and len(loaded) == len(shape)
        if ok and all(type(v) is t for v, t in zip(loaded, shape, strict=True)):
            return tuple(loaded)
    except ValueError:
        pass
    raise ApiError(ErrorCode.INVALID_REQUEST, "The cursor is not valid.", "cursor")
