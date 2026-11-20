"""The admin API (`/admin/v1`) as its own Starlette app, and the combiner for a shared port.

Metrics exposure (decided here, T-ADM-03): `/metrics` is served by this app, never by the
data-plane app. On a separate admin listener it is open to whoever can reach that port (the
perimeter is the port, as in protocol.md section 3); on a shared port it requires an admin key
with `usage:read`, because the data-plane port is reachable by every application.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from spendgate.core.errors import ErrorCode, Refusal
from spendgate.gateway import admin_budgets as budgets
from spendgate.gateway import admin_reports as reports
from spendgate.gateway import admin_scopes as scopes
from spendgate.gateway.admin_http import (
    AdminContext,
    Handler,
    authenticate,
    error_response,
    guarded,
)
from spendgate.gateway.observability import metrics_endpoint
from spendgate.ledger.key_types import AdminScope

READ, KEYS, MONEY = AdminScope.USAGE_READ, AdminScope.KEYS_WRITE, AdminScope.BUDGETS_WRITE
BASE = "/admin/v1"


@dataclass(frozen=True, slots=True)
class Op:
    method: str
    path: str
    scope: AdminScope
    handler: Handler


# More specific paths first: `{scope_id:path}` swallows slashes, so `/budgets` must come before it.
OPERATIONS: tuple[Op, ...] = (
    Op("GET", "/status", READ, reports.status),
    Op("GET", "/readiness", READ, reports.readiness),
    Op("GET", "/scopes", READ, scopes.list_scopes),
    Op("POST", "/scopes", KEYS, scopes.create_scope),
    Op("GET", "/scopes/{scope_id:path}/budgets", READ, budgets.list_budgets),
    Op("POST", "/scopes/{scope_id:path}/budgets", MONEY, budgets.create_budget),
    Op("GET", "/scopes/{scope_id:path}", READ, scopes.get_scope),
    Op("PATCH", "/scopes/{scope_id:path}", KEYS, scopes.update_scope),
    Op("GET", "/keys", READ, scopes.list_keys),
    Op("POST", "/keys", KEYS, scopes.create_key),
    Op("POST", "/keys/{key_id}/revoke", KEYS, scopes.revoke_key),
    Op("POST", "/keys/{key_id}/rotate", KEYS, scopes.rotate_key),
    Op("GET", "/keys/{key_id}", READ, scopes.get_key),
    Op("PATCH", "/keys/{key_id}", KEYS, scopes.update_key),
    Op("POST", "/budgets/{budget_id}/ack-overrun", MONEY, budgets.ack_overrun),
    Op("POST", "/budgets/{budget_id}/grants", MONEY, budgets.create_grant),
    Op("PATCH", "/budgets/{budget_id}", MONEY, budgets.update_budget),
    Op("DELETE", "/budgets/{budget_id}", MONEY, budgets.delete_budget),
    Op("GET", "/kill-switches", READ, budgets.list_kill_switches),
    Op("POST", "/kill-switches", MONEY, budgets.engage_kill_switch),
    Op("DELETE", "/kill-switches/{switch_id}", MONEY, budgets.lift_kill_switch),
    Op("GET", "/usage", READ, reports.query_usage),
    Op("GET", "/calls", READ, reports.list_calls),
    Op("GET", "/calls/{request_id}", READ, reports.get_call),
    Op("GET", "/audit", READ, reports.list_audit),
)


def _not_found(ctx: AdminContext) -> Callable[[Request], Awaitable[Response]]:
    async def view(request: Request) -> Response:
        refusal = Refusal(ErrorCode.NOT_FOUND, "No such admin endpoint.")
        return error_response(refusal, "sg_adm_unrouted")

    return view


def _unrouted(request: Request, exc: Exception) -> Response:
    """Starlette's `.*` convertors do not match a newline, so such a URL reaches neither a route
    nor the fallback and would get the plain-text 404 (FZ-02); this gives it the error shape."""
    return error_response(
        Refusal(ErrorCode.NOT_FOUND, "No such admin endpoint."), "sg_adm_unrouted"
    )


def _metrics_guard(ctx: AdminContext) -> Callable[[Request], Awaitable[Response | None]]:
    async def guard(request: Request) -> Response | None:
        found = await authenticate(ctx, request, READ)
        return error_response(found, "sg_adm_metrics") if isinstance(found, Refusal) else None

    return guard


def create_admin_app(ctx: AdminContext, *, shared_port: bool) -> Starlette:
    """Admin routes plus `/metrics` (when `ctx.metrics` is set); see the module docstring."""
    routes = [
        Route(BASE + op.path, guarded(ctx, op.scope, op.handler), methods=[op.method])
        for op in OPERATIONS
    ]
    if ctx.metrics is not None:
        guard = _metrics_guard(ctx) if shared_port else None
        routes.append(Route("/metrics", metrics_endpoint(ctx.metrics, guard), methods=["GET"]))
    routes.append(
        Route(
            "/admin/{path:path}", _not_found(ctx), methods=["GET", "POST", "PATCH", "PUT", "DELETE"]
        )
    )
    return Starlette(routes=routes, exception_handlers={404: _unrouted})


def combine(data_app: ASGIApp, admin_app: ASGIApp) -> ASGIApp:
    """One listener for both: `/admin/*` and `/metrics` go to the admin app, the rest (and the
    lifespan, which owns the heartbeat) to the data-plane app."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and (path == "/metrics" or path.startswith("/admin/")):
            await admin_app(scope, receive, send)
        else:
            await data_app(scope, receive, send)

    return app
