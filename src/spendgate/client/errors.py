"""Typed exceptions parsed from the protocol's error bodies (protocol.md section 4).

SDK errors are duck-typed (`status_code`, `body`, `response`), so this module imports neither
`openai` nor `anthropic`. Both body shapes are handled: OpenAI puts the error object at
`body["error"]` (the SDK may already unwrap it), Anthropic wraps it as
`{"type": "error", "error": {...}}`.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime

from spendgate.core.errors import ErrorCode, LimitName
from spendgate.core.money import Money, MoneyError, parse_amount

REQUEST_ID_HEADER = "x-spendgate-request-id"
_MAX_RETRY_DIGITS = 9  # also keeps `int` clear of its digit limit


class SpendgateError(Exception):
    """Any refusal from the spendgate gateway; `code` is the wire code (unknown codes kept)."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, str | int] | None = None,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: Mapping[str, str | int] = dict(details or {})
        self.status_code = status_code
        self.request_id = request_id

    def _text(self, name: str) -> str | None:
        value = self.details.get(name)
        return value if isinstance(value, str) else None

    def _seconds(self) -> int | None:
        value = self.details.get("retry_after_seconds")
        return value if isinstance(value, int) and not isinstance(value, bool) else None


class BudgetExhausted(SpendgateError):
    """`budget_exhausted` and `budget_period_exhausted`."""

    @property
    def scope(self) -> str | None:
        return self._text("scope")

    @property
    def remaining(self) -> Money | None:
        return self._money("remaining")

    @property
    def hold(self) -> Money | None:
        """The refused call's worst-case hold; absent on an overrun-review refusal."""
        return self._money("hold")

    def _money(self, name: str) -> Money | None:
        text = self._text(name)
        if text is None:
            return None
        try:
            return parse_amount(text)
        except MoneyError:
            return None

    @property
    def period_resets_at(self) -> datetime | None:
        text = self._text("period_resets_at")
        try:
            return datetime.fromisoformat(text) if text else None
        except ValueError:
            return None


class RateLimited(SpendgateError):
    """Our own RPM, TPM or concurrency limit (`details.limit` says which)."""

    @property
    def retry_after(self) -> int | None:
        return self._seconds()


class UpstreamThrottled(RateLimited):
    """Every candidate deployment was throttled by the provider."""


class ModelNotPriced(SpendgateError):
    """The model has no current catalog price; refused before upstream."""


class SessionLimitReached(SpendgateError):
    @property
    def limit(self) -> LimitName | None:
        text = self._text("limit")
        try:
            return LimitName(text) if text else None
        except ValueError:
            return None


class KillSwitch(SpendgateError):
    """A kill switch applies until an admin lifts it."""


class LedgerUnavailable(SpendgateError):
    @property
    def retry_after(self) -> int | None:
        return self._seconds()


_BY_CODE: Mapping[str, type[SpendgateError]] = {
    ErrorCode.BUDGET_EXHAUSTED: BudgetExhausted,
    ErrorCode.BUDGET_PERIOD_EXHAUSTED: BudgetExhausted,
    ErrorCode.RATE_LIMITED: RateLimited,
    ErrorCode.UPSTREAM_THROTTLED: UpstreamThrottled,
    ErrorCode.MODEL_NOT_PRICED: ModelNotPriced,
    ErrorCode.SESSION_LIMIT_REACHED: SessionLimitReached,
    ErrorCode.KILL_SWITCH: KillSwitch,
    ErrorCode.LEDGER_UNAVAILABLE: LedgerUnavailable,
}
_KNOWN = {str(code) for code in ErrorCode}


def _error_object(exc: object) -> Mapping[str, object] | None:
    body = getattr(exc, "body", None)
    if not isinstance(body, Mapping):
        response = getattr(exc, "response", None)
        try:
            body = response.json() if response is not None else None
        except ValueError:
            body = None
    if isinstance(body, Mapping) and isinstance(body.get("error"), Mapping):
        body = body["error"]
    return body if isinstance(body, Mapping) else None


def _headers(exc: object) -> Mapping[str, str]:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    return headers if isinstance(headers, Mapping) else {}


def from_error_body(
    body: Mapping[str, object],
    *,
    status_code: int | None = None,
    request_id: str | None = None,
    retry_after: int | None = None,
) -> SpendgateError | None:
    """Typed error from a protocol error object, or None when it is not a spendgate code."""
    code = body.get("code")
    if not isinstance(code, str) or (code not in _KNOWN and request_id is None):
        return None
    raw = body.get("details")
    details: dict[str, str | int] = (
        {k: v for k, v in raw.items() if isinstance(v, str | int)}
        if isinstance(raw, Mapping)
        else {}
    )
    if retry_after is not None:  # the Retry-After header backs up a missing detail
        details.setdefault("retry_after_seconds", retry_after)
    message = body.get("message")
    cls = _BY_CODE.get(code, SpendgateError)
    return cls(
        code,
        message if isinstance(message, str) else code,
        details,
        status_code=status_code,
        request_id=request_id,
    )


def _retry_after(raw: str | None) -> int | None:
    """Whole seconds from a delta-seconds header; anything else (a date, `²`, junk) is no hint.

    A local twin of the providers' parser: the client layer cannot import `providers`.
    """
    text = (raw or "").strip()
    if not (text.isascii() and text.isdigit()) or len(text) > _MAX_RETRY_DIGITS:
        return None
    return int(text)


def from_sdk_error(exc: object) -> SpendgateError | None:
    """Convert an `openai`/`anthropic` `APIStatusError` into a typed error, else None."""
    status = getattr(exc, "status_code", None)
    obj = _error_object(exc)
    if not isinstance(status, int) or obj is None:
        return None
    headers = _headers(exc)
    after = headers.get("retry-after")
    return from_error_body(
        obj,
        status_code=status,
        request_id=headers.get(REQUEST_ID_HEADER),
        retry_after=_retry_after(after),
    )


@contextmanager
def raise_for_spendgate() -> Iterator[None]:
    """Re-raise SDK status errors that came from spendgate as typed ones; others pass through."""
    try:
        yield
    except Exception as exc:
        typed = from_sdk_error(exc)
        if typed is None:
            raise
        raise typed from exc
