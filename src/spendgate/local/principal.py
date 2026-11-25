"""`LocalPrincipal`: who runs in library mode. No key store, no secret, one scope.

Library mode is cooperative (the process holds the provider key), so there is nothing to
authenticate: the principal is the script itself. It plugs into the pipeline as a `KeyVerifier`
that accepts any presented credential and answers with the same `Principal` and `KeyPolicy` shape
the gateway builds from a stored key, so every later step runs unchanged.

Session limits reach the ledger through `KeyPolicy.session_defaults`: the hold transaction copies
them onto the session row (and its own budget) when the session is first used.
"""

import math
from dataclasses import dataclass

from spendgate.client.session import SessionContext, current_session
from spendgate.core.errors import Refusal
from spendgate.core.money import parse_amount
from spendgate.core.policy import KeyPolicy
from spendgate.core.sessions import SessionDefaults
from spendgate.ledger.key_types import KeyKind, Principal

LOCAL_KEY_ID = "local"


def session_defaults(ctx: SessionContext | None, currency: str) -> SessionDefaults:
    """Limits of the active `spendgate.local.session(...)` block, in the ledger's currency.

    Never `None`: a session header with no limits still needs a (limitless) defaults object, or
    the ledger refuses the call. Raises `ValueError` for a bad limit, so the caller sees it at the
    call site instead of as a refused request.
    """
    if ctx is None or not ctx.has_limits:
        return SessionDefaults()
    budget_nano: int | None = None
    if ctx.budget is not None:
        budget = parse_amount(ctx.budget)
        if budget.currency != currency:
            raise ValueError(
                f"session budget {ctx.budget!r} is not in the ledger currency {currency}"
            )
        budget_nano = budget.nano
    return SessionDefaults(
        budget_nano=budget_nano,
        max_steps=ctx.max_steps,
        max_seconds=None if ctx.max_seconds is None else math.ceil(ctx.max_seconds),
        idle_seconds=None if ctx.idle_seconds is None else math.ceil(ctx.idle_seconds),
    )


@dataclass(frozen=True, slots=True)
class LocalPrincipal:
    """The in-process caller: a scope and an optional policy, verified without a secret."""

    scope_id: str
    currency: str
    key_id: str = LOCAL_KEY_ID
    allowed_models: tuple[str, ...] | None = None
    max_output_tokens: int | None = None
    per_call_ceiling_nano: int | None = None
    rpm: int | None = None
    tpm: int | None = None
    max_concurrent: int | None = None

    async def verify_api(self, raw: str, now: int) -> Principal | Refusal:
        """Accept any credential (the transport sends a fixed placeholder); never a refusal."""
        policy = KeyPolicy(
            self.key_id,
            self.scope_id,
            self.allowed_models,
            self.max_output_tokens,
            self.per_call_ceiling_nano,
            self.rpm,
            self.tpm,
            self.max_concurrent,
            session_defaults(current_session(), self.currency),
        )
        return Principal(self.key_id, KeyKind.API, self.scope_id, policy)

    async def still_active(self, principal: Principal, now: int) -> bool:
        """Always: the script's principal has no key that could be revoked or expire."""
        return True
