"""SDK helpers for proxy mode: configured clients, session context and typed errors.

Importing this package never imports `openai` or `anthropic`; each helper loads its SDK on use.
"""

from spendgate.client.anthropic import client as anthropic
from spendgate.client.errors import (
    BudgetExhausted,
    KillSwitch,
    LedgerUnavailable,
    ModelNotPriced,
    RateLimited,
    SessionLimitReached,
    SpendgateError,
    UpstreamThrottled,
    from_sdk_error,
    raise_for_spendgate,
)
from spendgate.client.openai import client as openai
from spendgate.client.session import SessionContext, activate_session, current_session, session

# `openai` and `anthropic` are the helper functions; they shadow the same-named submodules
# on purpose, so `spendgate.client.openai(...)` reads as in the docs.
__all__ = [
    "BudgetExhausted",
    "KillSwitch",
    "LedgerUnavailable",
    "ModelNotPriced",
    "RateLimited",
    "SessionContext",
    "SessionLimitReached",
    "SpendgateError",
    "UpstreamThrottled",
    "activate_session",
    "anthropic",
    "current_session",
    "from_sdk_error",
    "openai",
    "raise_for_spendgate",
    "session",
]
