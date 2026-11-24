"""Open the configured ledger and the key service over it (shared by serve, migrate, --local)."""

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from spendgate.config import Settings
from spendgate.ledger.admin_port import AdminLedger
from spendgate.ledger.keys import KeyService
from spendgate.pipeline.model import Clock


def system_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class Runtime:
    settings: Settings
    ledger: AdminLedger
    keys: KeyService
    now: Clock


@asynccontextmanager
async def open_runtime(
    settings: Settings, *, migrate: bool | None = None
) -> AsyncIterator[Runtime]:
    """Opens the ledger, closes it on exit. `migrate` None follows `ledger.auto_migrate`; when
    not migrating, a schema that is behind is refused (`SchemaBehind`, ADR-0018)."""
    if migrate is None:
        migrate = settings.config.ledger.migrates_on_start
    backend = settings.config.ledger.backend
    currency = settings.ledger_currency
    ledger: AdminLedger
    if backend.kind == "sqlite" and settings.ledger_path is not None:
        from spendgate.ledger.sqlite import SqliteLedger  # noqa: PLC0415 - only the chosen backend

        sqlite = SqliteLedger.open(settings.ledger_path, currency=currency, migrate=migrate)
        ledger = sqlite
        closer: Callable[[], Awaitable[None] | None] = sqlite.close
    elif settings.ledger_dsn is not None:
        from spendgate.ledger.postgres import PostgresLedger  # noqa: PLC0415 - optional extra

        pg = await PostgresLedger.open(
            settings.ledger_dsn.reveal(), currency=currency, migrate=migrate
        )
        ledger = pg
        closer = pg.aclose
    else:
        raise ValueError("the ledger backend has no location")
    try:
        keys = KeyService(ledger, settings.key_pepper.reveal().encode())
        yield Runtime(settings, ledger, keys, system_ms)
    finally:
        done = closer()
        if done is not None:
            await done
