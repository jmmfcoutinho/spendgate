"""A synchronous rig over any ledger adapter: one tiny world, plus helpers for the scenarios.

The contract tests are plain sync functions; the rig owns an event loop (a Postgres pool is bound
to one loop) and runs the adapter's coroutines on it. Money amounts are written as strings of
whole units ("9.40") and converted with `core.money`, so no float ever touches a test.
"""

import asyncio
import atexit
import hashlib
import os
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo

from spendgate.core.budgets import Budget, Period, ScopeKind
from spendgate.core.money import Money
from spendgate.core.outcomes import Outcome
from spendgate.core.policy import KeyPolicy
from spendgate.core.sessions import SessionDefaults
from spendgate.ledger.admin_port import AdminLedger
from spendgate.ledger.port import (
    AttemptRecord,
    BudgetTotals,
    Candidate,
    Held,
    HoldMeta,
    HoldRequest,
    Refused,
    ScopeRow,
    Settlement,
    Usage,
)
from spendgate.ledger.postgres import PostgresLedger
from spendgate.ledger.sqlite import SqliteLedger

T0 = 1_700_000_000_000  # an arbitrary UTC instant, epoch milliseconds
LEASE_MS = 60_000
SECOND = 1_000
CURRENCY = "EUR"
POSTGRES_HOOK_READY = True
POSTGRES_URL_ENV = "SPENDGATE_TEST_POSTGRES_URL"


def E(amount: str) -> int:
    """Whole units as a string ("9.40") to nano-units."""
    return Money.from_amount(amount, CURRENCY).nano


def open_ledger(
    kind: str,
    location: str,
    *,
    new_id: Callable[[], str] | None = None,
    synchronous: str = "FULL",
) -> AdminLedger:
    """Open an adapter on `location` (sqlite: a file path; postgres: a DSN naming its schema)."""
    kwargs: dict[str, Any] = {"currency": CURRENCY}
    if new_id is not None:
        kwargs["new_id"] = new_id
    if kind == "sqlite":
        return SqliteLedger.open(location, synchronous=synchronous, **kwargs)  # type: ignore[arg-type]
    if kind == "postgres":
        # A private loop: `open` only migrates; the pool is created later in the caller's loop.
        return asyncio.run(PostgresLedger.open(location, **kwargs))
    raise NotImplementedError(f"no contract-test hook for the {kind!r} adapter yet")


def close_ledger(ledger: AdminLedger) -> None:
    if isinstance(ledger, PostgresLedger):
        if ledger.loop is not None and not ledger.loop.is_closed():
            ledger.loop.run_until_complete(ledger.aclose())  # the pool is bound to that loop
        return
    close = getattr(ledger, "close", None)
    if close is not None:
        close()


def workdir_location(kind: str, directory: Path) -> str:
    """Where a fresh store for `kind` lives: a file, or a DSN whose search_path is a new schema."""
    if kind == "sqlite":
        return str(directory / "ledger.db")
    if kind == "postgres":
        return _postgres_schema_dsn(os.environ[POSTGRES_URL_ENV], directory)
    raise NotImplementedError(f"no contract-test hook for the {kind!r} adapter yet")


_schemas: list[tuple[str, str]] = []  # (server url, schema) created by this process


def _drop_schemas() -> None:
    for url, schema in _schemas:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


atexit.register(_drop_schemas)


def _postgres_schema_dsn(url: str, directory: Path) -> str:
    """One schema per test, named after its (unique) directory, so tests share a server safely.

    A schema, not a database: `CREATE SCHEMA` is a few ms where `CREATE DATABASE` is ~100 ms and
    needs more privilege, and `search_path` in the DSN makes the migrations and every statement
    land in it, also in spawned worker processes that only receive the DSN. Schemas are dropped
    when the test process exits.
    """
    schema = "sg_" + hashlib.sha1(str(directory).encode(), usedforsecurity=False).hexdigest()[:16]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    if (url, schema) not in _schemas:
        _schemas.append((url, schema))
    return make_conninfo(url, options=f"-c search_path={schema}")


@dataclass(frozen=True, slots=True)
class World:
    """A user scope with one budget and a key bound to it."""

    scope: ScopeRow
    budget: Budget
    key: KeyPolicy
    parent: ScopeRow
    extra: tuple[Budget, ...] = field(default=())


class Rig:
    def __init__(
        self, kind: str, location: str, new_id: Callable[[], str], synchronous: str = "FULL"
    ) -> None:
        self.kind, self.location, self.synchronous = kind, location, synchronous
        self._new_id = new_id
        self._loop = asyncio.new_event_loop()
        self.ledger = open_ledger(kind, location, new_id=new_id, synchronous=synchronous)

    def run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        return self._loop.run_until_complete(coro)

    def restart(self) -> None:
        """Close and reopen the same store: a process restart. Must not touch any hold."""
        close_ledger(self.ledger)
        self.ledger = open_ledger(
            self.kind, self.location, new_id=self._new_id, synchronous=self.synchronous
        )

    def close_connection(self) -> None:
        close_ledger(self.ledger)

    def close(self) -> None:
        close_ledger(self.ledger)
        self._loop.close()

    # --- setup ---------------------------------------------------------------------------

    def world(  # noqa: PLR0913 - a test world has this many knobs
        self,
        cap: str,
        *,
        period: Period = Period.NONE,
        strict_overrun: bool = True,
        session_defaults: SessionDefaults | None = None,
        key_id: str = "key-1",
        org_cap: str | None = None,
        now: int = T0,
    ) -> World:
        parent = self.run(self.ledger.create_scope(ScopeKind.ORG, "acme", None, now=now))
        scope = self.run(self.ledger.create_scope(ScopeKind.USER, "u1", parent.id, now=now))
        budget = self.run(
            self.ledger.create_budget(
                scope.id, period, E(cap), now=now, strict_overrun=strict_overrun
            )
        )
        extra: tuple[Budget, ...] = ()
        if org_cap is not None:
            extra = (
                self.run(self.ledger.create_budget(parent.id, Period.NONE, E(org_cap), now=now)),
            )
        self.run(self.ledger.add_key_record(key_id, scope.id, now=now))
        key = KeyPolicy(key_id, scope.id, session_defaults=session_defaults)
        return World(scope, budget, key, parent, extra)

    def add_key(self, w: World, key_id: str, defaults: SessionDefaults | None = None) -> KeyPolicy:
        self.run(self.ledger.add_key_record(key_id, w.scope.id, now=T0))
        return KeyPolicy(key_id, w.scope.id, session_defaults=defaults)

    # --- transitions ---------------------------------------------------------------------

    def request(  # noqa: PLR0913
        self,
        w: World,
        rid: str,
        worst: str,
        now: int = T0,
        *,
        key: KeyPolicy | None = None,
        session: str | None = None,
        lease_ms: int = LEASE_MS,
        instance: str = "A",
        model: str = "gpt",
        providers: tuple[str, ...] = ("p1",),
        labels: dict[str, str] | None = None,
        user_label: str | None = None,
    ) -> HoldRequest:
        meta = HoldMeta(
            "/v1/chat/completions",
            model,
            "sha-test",
            tuple(Candidate(p, f"{p}-dep", f"{p}-price") for p in providers),
            user_label,
            labels or {},
        )
        return HoldRequest(
            rid, instance, now, lease_ms, E(worst), key or w.key, model, providers, meta, session
        )

    def hold(self, w: World, rid: str, worst: str, now: int = T0, **kw: Any) -> Held | Refused:  # noqa: ANN401
        return self.run(self.ledger.hold(self.request(w, rid, worst, now, **kw)))

    def hold_ok(self, w: World, rid: str, worst: str, now: int = T0, **kw: Any) -> Held:  # noqa: ANN401
        result = self.hold(w, rid, worst, now, **kw)
        assert isinstance(result, Held), result
        return result

    def hold_nano(
        self,
        w: World,
        rid: str,
        worst_nano: int,
        now: int,
        **kw: Any,  # noqa: ANN401
    ) -> Held | Refused:
        """Like `hold` with the amount in nano-units (for generated amounts)."""
        request = replace(self.request(w, rid, "0", now, **kw), worst_nano=worst_nano)
        return self.run(self.ledger.hold(request))

    def settle_nano(self, rid: str, charge_nano: int, now: int) -> Settlement:
        made = (attempt(1, Outcome.OK, charge_nano, now, usage_cost_nano=charge_nano),)
        return self.run(self.ledger.settle(rid, charge_nano, made, now))

    def settle(  # noqa: PLR0913
        self,
        rid: str,
        charge: str,
        now: int = T0,
        *,
        outcome: Outcome = Outcome.OK,
        usage: str | None = None,
        attempts: tuple[AttemptRecord, ...] | None = None,
    ) -> Settlement:
        used = E(usage) if usage is not None else None
        made = attempts or (attempt(1, outcome, E(charge), now, usage_cost_nano=used),)
        return self.run(self.ledger.settle(rid, E(charge), made, now))

    def release(
        self, rid: str, now: int = T0, *, attempts: tuple[AttemptRecord, ...] | None = None
    ) -> Settlement:
        made = attempts if attempts is not None else (attempt(1, Outcome.THROTTLED, 0, now, True),)
        return self.run(self.ledger.release(rid, made, now))

    def disable_budget(self, budget_id: str) -> None:
        """Stop a budget applying (`disabled_at`); the admin API's delete is a soft delete."""
        self.run(self.ledger.delete_budget(budget_id, actor="admin", now=T0))

    def sweep(self, now: int) -> tuple[str, ...]:
        return self.run(self.ledger.sweep_expired(now)).expired

    def totals(self, w: World, now: int = T0, *, budget: Budget | None = None) -> BudgetTotals:
        target = budget or w.budget
        rows = self.run(self.ledger.totals(target.scope_id, now))
        return next(t for t in rows if t.budget_id == target.id)


def attempt(  # noqa: PLR0913 - a test helper with defaults for everything
    number: int,
    outcome: Outcome,
    charged: int,
    now: int = T0,
    not_processed: bool = False,
    *,
    usage_cost_nano: int | None = None,
    provider: str = "p1",
    deployment: str | None = None,
    http_status: int | None = None,
) -> AttemptRecord:
    return AttemptRecord(
        attempt=number,
        provider=provider,
        deployment=deployment or f"{provider}-dep{number}",
        price_id=f"{provider}-price",
        outcome=outcome,
        charged_nano=charged,
        started_at=now - 100,
        finished_at=now,
        not_processed=not_processed,
        usage_cost_nano=usage_cost_nano,
        usage=Usage(tokens_in=10, tokens_out=5) if usage_cost_nano is not None else Usage(),
        http_status=http_status,
    )


def hold_worker(  # noqa: PLR0913, PLR0917 - a spawned process gets everything as plain arguments
    kind: str,
    location: str,
    scope_id: str,
    key_id: str,
    prefix: str,
    count: int,
    worst: int,
    settle_to: int | None = None,
) -> int:
    """Child process of the multi-process test: `count` holds on one scope; returns the admitted."""
    ledger = open_ledger(kind, location)
    loop = asyncio.new_event_loop()
    try:
        key = KeyPolicy(key_id, scope_id)
        meta = HoldMeta("/v1/chat", "gpt", "sha", (Candidate("p1", "d", "price"),))
        admitted = 0
        for n in range(count):
            request = HoldRequest(
                f"{prefix}-{n}", prefix, T0, LEASE_MS, worst, key, "gpt", ("p1",), meta
            )
            if isinstance(loop.run_until_complete(ledger.hold(request)), Held):
                admitted += 1
                if settle_to is not None:  # a mixed workload: holds and settles interleave
                    done = (attempt(1, Outcome.OK, settle_to, T0, usage_cost_nano=settle_to),)
                    loop.run_until_complete(ledger.settle(request.request_id, settle_to, done, T0))
        return admitted
    finally:
        close_ledger(ledger)  # before the loop closes: a Postgres pool lives on that loop
        loop.close()
