"""`spendgate.local.session(...)`: a session block with limits, for library mode.

`spendgate.client.session(...)` (proxy mode) refuses limit arguments because the gateway takes
limits from the key's `session_defaults`. In library mode there is no key store, so the limits
are given here and travel with the call: the transport copies them into the local principal's
`session_defaults`, and the ledger copies those onto the session row the first time the session id
is used. A later block with the same id keeps the limits the session started with.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from spendgate.client.session import SessionContext, activate_session


@contextmanager
def session(
    session_id: str,
    budget: str | None = None,
    max_steps: int | None = None,
    max_seconds: float | None = None,
    idle_seconds: float | None = None,
) -> Iterator[SessionContext]:
    """Tag every call in the block with `session_id` and apply these limits to the session."""
    ctx = SessionContext(session_id, budget, max_steps, max_seconds, idle_seconds)
    with activate_session(ctx) as active:
        yield active
