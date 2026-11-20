"""Library mode: the same pipeline in-process, behind an httpx transport.

Cooperative, not a security boundary: code that holds the provider key can skip the guard. For
enforcement run the proxy. See `docs/library-mode.md`.
"""

from spendgate.local.guard import Guard, GuardError, guard
from spendgate.local.principal import LocalPrincipal
from spendgate.local.session import session
from spendgate.local.setup import CapMismatch, NoBudget, default_ledger_path
from spendgate.local.transport import AsyncLocalTransport, LocalTransport, transport

__all__ = [
    "AsyncLocalTransport", "CapMismatch", "Guard", "GuardError", "LocalPrincipal",
    "LocalTransport", "NoBudget", "default_ledger_path", "guard", "session", "transport",
]  # fmt: skip
