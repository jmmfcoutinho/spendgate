"""Open the configured ledger and the key service over it (shared by serve, migrate, --local)."""

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from spendgate.cli.client import EXIT_CONFIG, CliError
from spendgate.config import Settings
from spendgate.config.schema import EntraAuth, PostgresLedger, RdsIamAuth
from spendgate.ledger.admin_port import AdminLedger
from spendgate.ledger.keys import KeyService
from spendgate.pipeline.model import Clock

if TYPE_CHECKING:  # the Postgres modules need the optional extra
    from spendgate.ledger import postgres


def system_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class Runtime:
    settings: Settings
    ledger: AdminLedger
    keys: KeyService
    now: Clock


async def _postgres(settings: Settings, dsn: str, *, migrate: bool) -> "postgres.PostgresLedger":
    from spendgate.ledger.postgres import PostgresLedger as Ledger  # noqa: PLC0415 - optional
    from spendgate.ledger.postgres_auth import db_tokens  # noqa: PLC0415 - optional extra

    cfg = settings.config.ledger
    auth = cfg.backend.auth if isinstance(cfg.backend, PostgresLedger) else None
    password = None
    if auth is not None:
        region = auth.region if isinstance(auth, RdsIamAuth) else None
        scope = auth.scope if isinstance(auth, EntraAuth) else ""
        try:
            password = db_tokens(auth.kind, dsn, region=region, scope=scope).password
        except ValueError as exc:  # the DSN does not fit token auth: a config error, no traceback
            raise CliError(EXIT_CONFIG, f"{settings.source}: {exc}") from None
    return await Ledger.open(
        dsn,
        currency=settings.ledger_currency,
        min_size=cfg.pool_min,
        max_size=cfg.pool_max,
        pool_timeout=float(cfg.pool_timeout_seconds),
        password=password,
        migrate=migrate,
    )


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
        pg = await _postgres(settings, settings.ledger_dsn.reveal(), migrate=migrate)
        ledger, closer = pg, pg.aclose
    else:
        raise ValueError("the ledger backend has no location")
    try:
        keys = KeyService(ledger, settings.key_pepper.reveal().encode())
        yield Runtime(settings, ledger, keys, system_ms)
    finally:
        done = closer()
        if done is not None:
            await done
