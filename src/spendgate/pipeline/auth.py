"""Step 2: find the presented key and verify it. Keys never leave this function."""

from collections.abc import Mapping

from spendgate.core.errors import ErrorCode, Refusal
from spendgate.ledger.key_types import Principal
from spendgate.ledger.port import LedgerError
from spendgate.pipeline.model import KeyVerifier

_UNAUTHORIZED = Refusal(ErrorCode.UNAUTHORIZED, "Invalid or missing API key.")
_UNAVAILABLE = Refusal(
    ErrorCode.LEDGER_UNAVAILABLE, "The ledger is unavailable; try again.", retry_after_seconds=5
)
_BEARER = "bearer "


def presented_key(headers: Mapping[str, str]) -> str | Refusal:
    """`Authorization: Bearer` or `x-api-key`; when both are sent they must be the same key."""
    bearer: str | None = None
    auth = headers.get("authorization", "")
    if auth.lower().startswith(_BEARER):
        bearer = auth[len(_BEARER) :].strip()
    api_key = headers.get("x-api-key")
    if bearer and api_key and bearer != api_key.strip():
        return _UNAUTHORIZED
    found = bearer or (api_key.strip() if api_key else "")
    return found or _UNAUTHORIZED


async def authenticate(
    verifier: KeyVerifier, headers: Mapping[str, str], now: int
) -> Principal | Refusal:
    """The calling principal, `unauthorized`, or `ledger_unavailable` (fail closed)."""
    key = presented_key(headers)
    if isinstance(key, Refusal):
        return key
    try:
        return await verifier.verify_api(key, now)
    except LedgerError:
        return _UNAVAILABLE


async def recheck(verifier: KeyVerifier, who: Principal, now: int) -> Refusal | None:
    """None if `who`'s key is still active; else the refusal `authenticate` would give now.

    Run after the body upload, which can take minutes: a key revoked or expired meanwhile is
    refused before anything is held. The principal itself stays the one `authenticate` made.
    """
    try:
        return None if await verifier.still_active(who, now) else _UNAUTHORIZED
    except LedgerError:
        return _UNAVAILABLE
