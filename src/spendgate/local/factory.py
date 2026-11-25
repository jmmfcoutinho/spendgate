"""The runtime builder behind `transport()` and `transport2()`.

The httpx and httpx2 transports differ only in their base classes (each SDK refuses the other's
objects), so the `Runtime` they wrap is built here once from the options both entry points take.
"""

from pathlib import Path

import httpx

from spendgate.config import Settings, load
from spendgate.core.money import Money, parse_amount
from spendgate.local.runtime import Runtime, local_env
from spendgate.pipeline.model import Clock


def make_runtime(
    settings_or_path: Settings | str | Path,
    ledger_path: str | Path | None,
    cap: str | None,
    client: httpx.AsyncClient | None,
    now_ms: Clock,
) -> Runtime:
    settings = (
        settings_or_path
        if isinstance(settings_or_path, Settings)
        else load(settings_or_path, env=local_env())
    )
    amount: Money | None = parse_amount(cap) if cap is not None else None
    path = Path(ledger_path) if ledger_path is not None else None
    return Runtime(settings, path, amount, client=client, now_ms=now_ms)
