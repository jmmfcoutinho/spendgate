"""Admin routes for budgets, grants, overrun acknowledgement and kill switches."""

from typing import get_args

from starlette.responses import Response

from spendgate.config.schema import ProviderSlug
from spendgate.core.budgets import Grant, Period
from spendgate.core.errors import ErrorCode
from spendgate.core.policy import KillSwitch, KillTarget, model_filter_matches
from spendgate.gateway.admin_codec import budget_json, kill_json
from spendgate.gateway.admin_http import (
    ApiError,
    Call,
    iso,
    money_json,
    nano_of,
    no_content,
    respond,
)
from spendgate.gateway.admin_models import (
    AckIn,
    BudgetCreateIn,
    BudgetUpdateIn,
    GrantCreateIn,
    KillSwitchCreateIn,
)
from spendgate.gateway.admin_scopes import need_scope
from spendgate.ledger.admin_port import BudgetView
from spendgate.ledger.port import NotFoundError


async def need_budget(call: Call, budget_id: str) -> BudgetView:
    view = await call.ctx.ledger.get_budget(budget_id, call.now)
    if view is None or view.disabled:
        raise NotFoundError(f"budget {budget_id} does not exist")
    return view


async def _shown(call: Call, budget_id: str) -> dict[str, object]:
    return budget_json(await need_budget(call, budget_id), call.ctx.currency)


async def list_budgets(call: Call) -> Response:
    scope = await need_scope(call, call.path("scope_id"))
    views = await call.ctx.ledger.list_budgets(scope.id, call.now)
    return respond({"data": [budget_json(v, call.ctx.currency) for v in views]})


def _check_model_filter(call: Call, pattern: str | None) -> None:
    """Refuse a filter that matches no routed catalog id: such a budget would never apply.

    The usual cause is a filter written against an alias; the budget then guards nothing
    while looking configured, so it is refused here rather than stored.
    """
    if pattern is None:
        return
    routed = {r.catalog_id for r in call.ctx.settings.routes.values()}
    if not any(model_filter_matches(pattern, m) for m in routed):
        raise ApiError(
            ErrorCode.INVALID_REQUEST,
            "model_filter matches no routed catalog id; it is a glob over catalog ids such as "
            "openai/gpt-*, not over aliases.",
            "model_filter",
        )


async def create_budget(call: Call) -> Response:
    scope = await need_scope(call, call.path("scope_id"))
    body = await call.body(BudgetCreateIn)
    _check_model_filter(call, body.model_filter)
    strict = body.strict_overrun
    if strict is None:
        strict = call.ctx.settings.config.defaults.strict_overrun
    made = await call.ctx.ledger.create_budget(
        scope.id, Period(body.period), nano_of(call.ctx, body.cap, "cap"), now=call.now,
        soft_pct=body.soft_pct, model_filter=body.model_filter, strict_overrun=strict,
        note=body.note, actor=call.actor,
    )  # fmt: skip
    return respond(await _shown(call, made.id), 201)


async def update_budget(call: Call) -> Response:
    budget_id = call.path("budget_id")
    await need_budget(call, budget_id)
    body = await call.body(BudgetUpdateIn)
    cap = None if body.cap is None else nano_of(call.ctx, body.cap, "cap")
    await call.ctx.ledger.update_budget(
        budget_id, actor=call.actor, now=call.now, cap_nano=cap, soft_pct=body.soft_pct,
        strict_overrun=body.strict_overrun, note=body.note,
    )  # fmt: skip
    return respond(await _shown(call, budget_id))


async def delete_budget(call: Call) -> Response:
    budget_id = call.path("budget_id")
    await need_budget(call, budget_id)
    await call.ctx.ledger.delete_budget(budget_id, actor=call.actor, now=call.now)
    return no_content()


async def create_grant(call: Call) -> Response:
    budget_id = call.path("budget_id")
    await need_budget(call, budget_id)
    body = await call.body(GrantCreateIn)
    start = call.now if body.valid_from is None else body.valid_from
    amount = nano_of(call.ctx, body.amount, "amount")
    try:
        Grant(amount, start, body.valid_until)  # the same validation the cap arithmetic uses
    except ValueError:
        raise ApiError(
            ErrorCode.INVALID_REQUEST,
            "A grant needs a positive amount and valid_until after valid_from.",
            "valid_until",
        ) from None
    grant_id = await call.ctx.ledger.create_grant(
        budget_id, amount_nano=amount, valid_from=start, valid_until=body.valid_until,
        actor=call.actor, reason=body.reason, now=call.now,
    )  # fmt: skip
    return respond(
        {
            "id": grant_id, "budget_id": budget_id,
            "amount": money_json(amount, call.ctx.currency),
            "valid_from": iso(start), "valid_until": iso(body.valid_until),
            "reason": body.reason, "created_by": call.actor, "created_at": iso(call.now),
        },
        201,
    )  # fmt: skip


async def ack_overrun(call: Call) -> Response:
    budget_id = call.path("budget_id")
    await need_budget(call, budget_id)
    body = await call.body(AckIn)
    await call.ctx.ledger.ack_overrun(budget_id, actor=call.actor, reason=body.reason, now=call.now)
    return respond(await _shown(call, budget_id))


# --- kill switches ---------------------------------------------------------------------------


async def _target_ref(call: Call, kind: KillTarget, ref: str | None) -> str:
    """Validate and normalise `target_ref` (a scope path becomes its id)."""
    if kind is KillTarget.GLOBAL:
        if ref:
            raise ApiError(
                ErrorCode.INVALID_REQUEST, "A global switch has no target_ref.", "target_ref"
            )
        return ""
    if not ref:
        raise ApiError(ErrorCode.INVALID_REQUEST, "target_ref is required.", "target_ref")
    if kind is KillTarget.SCOPE:
        return (await need_scope(call, ref)).id
    if kind is KillTarget.KEY:
        found = await call.ctx.ledger.get_key(ref)
        if found is None:
            raise ApiError(ErrorCode.NOT_FOUND, "The key does not exist.", "target_ref")
        return found.id
    if kind is KillTarget.PROVIDER:
        known = set(get_args(ProviderSlug))
    else:
        known = {e.id for e in call.ctx.settings.catalog.entries()}
    if ref not in known:
        raise ApiError(ErrorCode.INVALID_REQUEST, f"Unknown {kind.value}.", "target_ref")
    return ref


async def list_kill_switches(call: Call) -> Response:
    rows = await call.ctx.ledger.list_kill_switches()
    return respond({"data": [kill_json(r) for r in rows]})


async def engage_kill_switch(call: Call) -> Response:
    body = await call.body(KillSwitchCreateIn)
    kind = KillTarget(body.target_kind)
    switch = KillSwitch(kind, await _target_ref(call, kind, body.target_ref))
    switch_id = await call.ctx.ledger.engage_kill_switch(
        switch, actor=call.actor, reason=body.reason, now=call.now
    )
    out: dict[str, object] = {
        "id": switch_id, "target_kind": kind.value, "reason": body.reason,
        "created_by": call.actor, "created_at": iso(call.now), "cleared_at": None,
    }  # fmt: skip
    if kind is not KillTarget.GLOBAL:
        out["target_ref"] = switch.target_ref
    return respond(out, 201)


async def lift_kill_switch(call: Call) -> Response:
    await call.ctx.ledger.lift_kill_switch(call.path("switch_id"), actor=call.actor, now=call.now)
    return no_content()
