"""The admission transaction (ledger-state-machine.md, "Admission"): one flow for both backends."""

from dataclasses import dataclass
from fnmatch import fnmatchcase

from spendgate.core.budgets import Admission, ScopeKind, check_admission, period_start
from spendgate.core.errors import ErrorCode, LimitName, Refusal
from spendgate.core.policy import KillSwitch, KillTarget, matching_kill_switch
from spendgate.core.sessions import SessionAction, SessionState, gate_session, session_path
from spendgate.ledger import sql
from spendgate.ledger.common import (
    HOUR_MS,
    Ctx,
    ensure_period,
    i,
    lock_budget,
    normalise_budgets,
    oi,
    s,
)
from spendgate.ledger.port import (
    ConflictError,
    Held,
    HeldItem,
    HoldRequest,
    LedgerError,
    NotFoundError,
    Refused,
)
from spendgate.ledger.sql import Flow, Row, Tx, q


@dataclass(frozen=True, slots=True)
class _Scope:
    id: str
    kind: ScopeKind
    path: str
    disabled_at: int | None = None


@dataclass(frozen=True, slots=True)
class _Session:
    scope: _Scope
    existed: bool  # False = created in this transaction, so a rollback removes it


type _Check = tuple[str, int, Admission]  # budget id, period start, verdict


def hold_flow(ctx: Ctx, req: HoldRequest) -> Flow[Held | Refused]:
    """Check kill switches, the session and every budget, then insert the hold, atomically.

    Locks are taken in the documented order: session scope row, then budget rows ascending.
    Any refusal rolls back the whole hold (no step, no session row, no partial hold).
    """
    if req.worst_nano < 0:
        raise ValueError("worst_nano must not be negative")
    if (yield q(sql.RES_EXISTS, id=req.request_id)):
        raise ConflictError(f"reservation {req.request_id} already exists")
    chain = yield from _chain(req.key.scope_id)
    for scope in chain:
        if scope.disabled_at is not None and scope.disabled_at <= req.now:
            return _forbidden("scope_disabled", f"Scope {scope.path} is disabled.", scope.path)
    if (killed := (yield from _kill_switch(req, chain))) is not None:
        return killed
    session: _Session | None = None
    if req.session is not None:
        admitted = yield from _admit_session(ctx, req, chain[0])
        if isinstance(admitted, Refused):
            return admitted
        session = admitted
    leaf = session.scope if session else chain[0]
    scopes = {c.id: c for c in [leaf, *chain]}
    owners = yield from _budget_owners(req, leaf.id)
    checks: list[_Check] = []
    for budget_id, scope_id in owners.items():  # ascending id: the lock order
        owner = scopes.get(scope_id)
        hint = (owner.kind, owner.path) if owner else None
        budget = yield from lock_budget(budget_id, hint, skip_disabled=True)
        if budget is None:
            continue
        start = period_start(budget.period, req.now)
        state = yield from ensure_period(budget_id, start)
        verdict = check_admission(budget, state, req.worst_nano, req.now, ctx.currency)
        if verdict.refusal is not None:
            return (yield from _refuse(req, verdict.refusal, list(owners), session))
        checks.append((budget_id, start, verdict))
    if not checks:  # no active budget would mean no limit at all: fail closed (ADR-0003)
        yield Tx.ROLLBACK  # drop a session row created above
        return _forbidden("no_budget", "No active budget applies to this key.", leaf.path)
    return (yield from _admit(req, leaf, session, checks))


def _forbidden(reason: str, message: str, scope: str) -> Refused:
    details = {"reason": reason, "scope": scope}
    return Refused(Refusal(ErrorCode.FORBIDDEN, message, None, details))


def _chain(leaf_id: str) -> Flow[list[_Scope]]:
    rows = yield q(sql.CHAIN, leaf=leaf_id)
    if not rows:
        raise NotFoundError(f"scope {leaf_id} does not exist")
    return [
        _Scope(s(r, "id"), ScopeKind(s(r, "kind")), s(r, "path"), oi(r, "disabled_at"))
        for r in rows
    ]


def _kill_switch(req: HoldRequest, chain: list[_Scope]) -> Flow[Refused | None]:
    rows = yield q(sql.KILL_ACTIVE)
    switches = [KillSwitch(KillTarget(s(r, "target_kind")), s(r, "target_ref")) for r in rows]
    hit = matching_kill_switch(switches, req.key, [c.id for c in chain], req.model, req.providers)
    if hit is None:
        return None
    return Refused(
        Refusal(ErrorCode.KILL_SWITCH, f"A {hit.target_kind.value} kill switch applies.")
    )


def _budget_owners(req: HoldRequest, leaf_id: str) -> Flow[dict[str, str]]:
    """Budget id -> owning scope id, ascending by id: the chain plus any budget the plan names."""
    rows = yield q(sql.CHAIN_BUDGETS, leaf=leaf_id)
    wanted: dict[str, str] = {}
    for row in rows:
        pattern = row["model_filter"]
        if pattern is None or (isinstance(pattern, str) and fnmatchcase(req.model, pattern)):
            wanted[s(row, "id")] = s(row, "scope_id")
    if req.plan is not None:
        for planned in req.plan.budgets:  # a stale plan may add a budget, never remove one
            wanted.setdefault(planned.budget.id, planned.budget.scope_id)
    return dict(sorted(wanted.items()))


# --- sessions --------------------------------------------------------------------------------


def _state_of(row: Row) -> SessionState:
    reason = row["closed_reason"]
    return SessionState(
        i(row, "started_at"),
        i(row, "last_activity_at"),
        i(row, "step_count"),
        oi(row, "max_steps"),
        oi(row, "max_seconds"),
        oi(row, "idle_seconds"),
        oi(row, "closed_at"),
        LimitName(reason) if isinstance(reason, str) else None,
    )


def _admit_session(ctx: Ctx, req: HoldRequest, parent: _Scope) -> Flow[_Session | Refused]:
    defaults = req.key.session_defaults
    if defaults is None or req.session is None:
        raise ValueError("a session needs a key with session_defaults")
    path = session_path(parent.path, req.key.id, req.session)
    found = yield q(sql.SCOPE_BY_PATH, path=path)
    prefix = f"{req.key.id}:"
    count = yield q(
        sql.SESSION_NEW_COUNT,
        parent=parent.id,
        plen=len(prefix),
        prefix=prefix,
        since=req.now - HOUR_MS,
    )
    old = found[0] if found else None
    decision = gate_session(defaults, _state_of(old) if old else None, req.now, i(count[0], "n"))
    if decision.refusal is not None:
        limit = LimitName(str(decision.refusal.details["limit"]))
        # A new-session rate refusal never closes the (idle, restartable) session.
        if old is not None and old["closed_at"] is None and limit is not LimitName.NEW_SESSIONS:
            yield from close_if_final(s(old, "id"), limit, req.now)
        return Refused(decision.refusal)
    if decision.action is SessionAction.CONTINUE:
        if old is None:  # CONTINUE only follows an existing row
            raise LedgerError("session state machine: CONTINUE without a session row")
        return _Session(_Scope(s(old, "id"), ScopeKind.SESSION, path), existed=True)
    if decision.action is SessionAction.RESTART:
        if old is None:
            raise LedgerError("session state machine: RESTART without a session row")
        yield q(sql.SESSION_RENAME, id=s(old, "id"), now=req.now, suffix=str(i(old, "started_at")))
    new = decision.new_state
    if new is None:  # CREATE and RESTART always carry a fresh state
        raise LedgerError("session state machine: no new session state to create")
    scope = yield from _create_session(ctx, req, parent, path, new)
    return _Session(scope, existed=False)


def _create_session(
    ctx: Ctx, req: HoldRequest, parent: _Scope, path: str, new: SessionState
) -> Flow[_Scope]:
    """Insert the session row (limits copied from the key) and its own budget, if any."""
    scope_id = ctx.new_id()
    yield q(
        sql.SCOPE_INSERT,
        id=scope_id,
        kind=ScopeKind.SESSION.value,
        parent_id=parent.id,
        name=f"{req.key.id}:{req.session}",
        path=path,
        now=req.now,
        max_steps=new.max_steps,
        max_seconds=new.max_seconds,
        idle_seconds=new.idle_seconds,
        started_at=new.started_at,
        last_activity_at=new.last_activity_at,
    )
    budget = req.key.session_defaults.budget_nano if req.key.session_defaults else None
    if budget is not None:
        yield q(
            sql.BUDGET_INSERT,
            id=ctx.new_id(),
            scope_id=scope_id,
            period="none",
            cap=budget,
            soft_pct=80,
            model_filter=None,
            strict=1,
            note=None,
            now=req.now,
        )
    return _Scope(scope_id, ScopeKind.SESSION, path)


def close_if_final(scope_id: str, limit: LimitName, now: int) -> Flow[None]:
    """Close a session that hit a limit, unless another of its requests is still in flight.

    A pending request may still be released, which refunds its step and hold (scenario 9), so
    steps and budget limits are final only when nothing is pending. Time cannot be refunded.
    """
    yield q(sql.SCOPE_LOCK, id=scope_id)
    if limit is not LimitName.TIME:
        pending = yield q(sql.SESSION_PENDING, id=scope_id)
        if i(pending[0], "n") > 0:
            return
    yield q(sql.SESSION_CLOSE, id=scope_id, now=now, reason=limit.value)


# --- outcome of the budget loop --------------------------------------------------------------


def _refuse(
    req: HoldRequest, refusal: Refusal, budget_ids: list[str], session: _Session | None
) -> Flow[Refused]:
    """Roll the hold back, then persist what must outlive a refusal in its own transaction.

    That is the recorded `excess` (A1: normalise_chain) and a session that hit its own budget.
    """
    yield Tx.ROLLBACK
    yield Tx.BEGIN
    own_budget = refusal.code is ErrorCode.SESSION_LIMIT_REACHED
    if own_budget and session is not None and session.existed:
        yield from close_if_final(session.scope.id, LimitName.BUDGET, req.now)
    yield from normalise_budgets(budget_ids, req.now)
    yield Tx.COMMIT
    return Refused(refusal)


def _admit(
    req: HoldRequest, leaf: _Scope, session: _Session | None, checks: list[_Check]
) -> Flow[Held]:
    items: list[HeldItem] = []
    for budget_id, start, verdict in checks:
        soft = req.now if verdict.warning is not None else None
        yield q(
            sql.PERIOD_HOLD,
            budget_id=budget_id,
            period_start=start,
            worst=req.worst_nano,
            excess=verdict.excess_nano,
            soft=soft,
        )
        items.append(HeldItem(budget_id, start, req.worst_nano))
    lease_until = req.now + req.lease_ms
    yield q(
        sql.RES_INSERT,
        id=req.request_id,
        owner=req.instance,
        lease=lease_until,
        key_id=req.key.id,
        scope_id=leaf.id,
        session_id=session.scope.id if session else None,
        meta=req.meta.to_json(),
        now=req.now,
    )
    for item in items:
        yield q(
            sql.ITEM_INSERT,
            id=req.request_id,
            budget_id=item.budget_id,
            period_start=item.period_start,
            held=item.held_nano,
        )
    if session is not None:
        yield q(sql.SESSION_TOUCH, id=session.scope.id, now=req.now)
    warnings = tuple(v.warning for _, _, v in checks if v.warning is not None)
    return Held(
        req.request_id, lease_until, tuple(items), session.scope.id if session else None, warnings
    )
