# ruff: noqa: ANN401
"""Shared rig for the admin API tests: the gateway stack plus an admin app over its ledger."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from spendgate.gateway.admin_http import AdminContext
from spendgate.gateway.metrics import Metrics
from spendgate.ledger.key_types import AdminScope
from tests.integration.harness import NOW_MS, PEPPER, Stack, open_stack

ALL = frozenset(AdminScope)


@dataclass(slots=True)
class Admin:
    stack: Stack
    ctx: AdminContext
    client: httpx.AsyncClient
    secret: str  # an admin key holding every scope
    key_id: str

    async def call(
        self, method: str, path: str, *, key: str | None = None, **kw: Any
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {key or self.secret}"} if key != "" else {}
        return await self.client.request(method, path, headers=headers, **kw)

    async def ok(self, method: str, path: str, status: int = 200, **kw: Any) -> dict[str, object]:
        r = await self.call(method, path, **kw)
        assert r.status_code == status, r.text
        body: dict[str, object] = r.json() if r.content else {}
        return body

    async def error(self, method: str, path: str, status: int, **kw: Any) -> dict[str, object]:
        r = await self.call(method, path, **kw)
        assert r.status_code == status, r.text
        error: dict[str, object] = r.json()["error"]
        return error

    async def admin_key(self, *scopes: AdminScope) -> str:
        made = await self.stack.ledger.create_admin_key(
            self.stack.scope.id, "limited", frozenset(scopes), pepper=PEPPER, actor="boot",
            now=NOW_MS,
        )  # fmt: skip
        return made.secret


@asynccontextmanager
async def open_admin(
    tmp_path: Path, *, shared_port: bool = True, **kw: Any
) -> AsyncIterator[Admin]:
    # The admin app `build_gateway` wires, as `serve` runs it; a separate port is a config entry.
    admin = None if shared_port else "127.0.0.1:8081"
    async with open_stack(tmp_path, metrics=Metrics(), admin_listen=admin, **kw) as s:
        ctx, app = s.gateway.admin_context, s.gateway.admin_app
        made = await s.ledger.create_admin_key(
            s.scope.id, "ops", ALL, pepper=PEPPER, actor="boot", now=NOW_MS
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://admin.test"
        )
        try:
            yield Admin(s, ctx, client, made.secret, made.record.id)
        finally:
            await client.aclose()
