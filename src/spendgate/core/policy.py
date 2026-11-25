"""Which budgets, limits and kill switches a call must pass: data in, decision out.

`resolve` never reads a clock or a store. The ledger passes the budgets bound to the key's
scope chain (leaf scope, its ancestors, the session scope) and the active kill switches;
labels (user, tags) are recorded for attribution and can never add or remove a budget.
"""

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase

from spendgate.core.budgets import Budget, period_start
from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.sessions import (
    SessionDecision,
    SessionDefaults,
    SessionState,
    gate_session,
)

SESSION_HEADER = "X-Spendgate-Session"
USER_HEADER = "X-Spendgate-User"
TAGS_HEADER = "X-Spendgate-Tags"
_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_TAG = re.compile(r"[a-z0-9._:-]{1,32}")
_MAX_TAGS = 10


class KillTarget(StrEnum):
    GLOBAL = "global"
    SCOPE = "scope"
    KEY = "key"
    PROVIDER = "provider"
    MODEL = "model"


@dataclass(frozen=True, slots=True)
class KillSwitch:
    target_kind: KillTarget
    target_ref: str = ""  # scope id, key id, provider slug or catalog model id; '' for global

    def __post_init__(self) -> None:
        if (self.target_kind is KillTarget.GLOBAL) != (self.target_ref == ""):
            raise ValueError("a global kill switch has no target; every other kind needs one")


@dataclass(frozen=True, slots=True)
class KeyPolicy:
    id: str
    scope_id: str
    allowed_models: tuple[str, ...] | None = None  # globs; None = every model
    max_output_tokens: int | None = None
    per_call_ceiling_nano: int | None = None
    rpm: int | None = None
    tpm: int | None = None
    max_concurrent: int | None = None
    session_defaults: SessionDefaults | None = None


@dataclass(frozen=True, slots=True)
class Labels:
    """Request labels as sent. Attribution only, except `session`, which selects a session."""

    session: str | None = None
    user: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Limits:
    rpm: int | None
    tpm: int | None
    max_concurrent: int | None


@dataclass(frozen=True, slots=True)
class PlannedBudget:
    budget: Budget
    period_start: int  # the period row the hold lives in, for its whole life


@dataclass(frozen=True, slots=True)
class Plan:
    budgets: tuple[PlannedBudget, ...]  # ordered by budget id (the lock order)
    limits: Limits
    per_call_ceiling_nano: int | None
    max_output_tokens: int | None
    session: SessionDecision | None


def check_labels(labels: Labels) -> Refusal | None:
    """Invalid label values are refused, never dropped or truncated."""

    def bad(param: str) -> Refusal:
        return Refusal(ErrorCode.INVALID_REQUEST, f"Invalid value for {param}.", param)

    if labels.session is not None and not _ID.fullmatch(labels.session):
        return bad(SESSION_HEADER)
    if labels.user is not None and not _ID.fullmatch(labels.user):
        return bad(USER_HEADER)
    if len(labels.tags) > _MAX_TAGS or not all(_TAG.fullmatch(t) for t in labels.tags):
        return bad(TAGS_HEADER)
    return None


def model_allowed(key: KeyPolicy, model: str) -> bool:
    return key.allowed_models is None or any(fnmatchcase(model, g) for g in key.allowed_models)


def matching_kill_switch(
    switches: Collection[KillSwitch],
    key: KeyPolicy,
    chain_scope_ids: Collection[str],
    model: str,
    providers: Sequence[str],
) -> KillSwitch | None:
    """First active switch that applies; a provider switch needs every candidate killed."""
    killed_providers = {s.target_ref for s in switches if s.target_kind is KillTarget.PROVIDER}
    if providers and all(p in killed_providers for p in providers):
        return KillSwitch(KillTarget.PROVIDER, providers[0])
    for switch in switches:
        ref = switch.target_ref
        hit = {
            KillTarget.GLOBAL: True,
            KillTarget.SCOPE: ref in chain_scope_ids,
            KillTarget.KEY: ref == key.id,
            KillTarget.MODEL: ref == model,
            KillTarget.PROVIDER: False,
        }[switch.target_kind]
        if hit:
            return switch
    return None


def model_filter_matches(pattern: str | None, catalog_id: str) -> bool:
    """A budget's `model_filter` is a case-sensitive glob over the catalog id (`openai/gpt-*`).

    Never over the alias the client sent: aliases are config names (several may route to one
    catalog id), while the catalog id names the priced model. None matches every model.
    """
    return pattern is None or fnmatchcase(catalog_id, pattern)


def select_budgets(
    budgets: Collection[Budget], chain_scope_ids: Collection[str], model: str, now: int
) -> tuple[PlannedBudget, ...]:
    """Budgets on the chain whose model filter matches, ordered by id."""
    chosen = [
        b
        for b in budgets
        if b.scope_id in chain_scope_ids and model_filter_matches(b.model_filter, model)
    ]
    return tuple(PlannedBudget(b, period_start(b.period, now)) for b in sorted(chosen, key=_id))


def _id(budget: Budget) -> str:
    return budget.id


def _session_header_refusal(key: KeyPolicy, labels: Labels) -> Refusal | None:
    """A key without session defaults refuses the header; a `session_required` key needs it.

    Without the header no session limit applies, so a required one is refused here: before the
    rate and concurrency limits are counted and before any hold.
    """
    defaults = key.session_defaults
    if labels.session is not None and defaults is None:
        return Refusal(
            ErrorCode.INVALID_REQUEST, "This key does not allow sessions.", SESSION_HEADER
        )
    if labels.session is None and defaults is not None and defaults.required:
        return Refusal(
            ErrorCode.INVALID_REQUEST, f"This key requires the {SESSION_HEADER} header.",
            SESSION_HEADER,
        )  # fmt: skip
    return None


def _gate(
    key: KeyPolicy, labels: Labels, session: SessionState | None, now: int, new_last_hour: int
) -> SessionDecision | None:
    if labels.session is None or key.session_defaults is None:
        return None
    return gate_session(key.session_defaults, session, now, new_last_hour)


def resolve(  # noqa: PLR0913 - one decision needs all of these inputs
    key: KeyPolicy,
    labels: Labels,
    model: str,
    now: int,
    *,
    budgets: Collection[Budget],
    chain_scope_ids: Collection[str],
    kill_switches: Collection[KillSwitch] = (),
    providers: Sequence[str] = (),
    session: SessionState | None = None,
    new_sessions_last_hour: int = 0,
) -> Plan | Refusal:
    """The budgets to hold, the limits to admit and the per-call ceiling, or the refusal.

    Check order: label syntax, session header allowed (or required), model allowed, kill
    switches, session gate. The ledger still re-checks kill switches and sessions inside the
    admission transaction.
    """
    if (bad := check_labels(labels)) is not None:
        return bad
    if (bad := _session_header_refusal(key, labels)) is not None:
        return bad
    if not model_allowed(key, model):
        return Refusal(ErrorCode.MODEL_NOT_ALLOWED, f"Model {model} is not allowed for this key.")
    if switch := matching_kill_switch(kill_switches, key, chain_scope_ids, model, providers):
        return Refusal(ErrorCode.KILL_SWITCH, f"A {switch.target_kind.value} kill switch applies.")
    gate = _gate(key, labels, session, now, new_sessions_last_hour)
    if gate is not None and gate.refusal is not None:
        return gate.refusal
    return Plan(
        select_budgets(budgets, chain_scope_ids, model, now),
        Limits(key.rpm, key.tpm, key.max_concurrent),
        key.per_call_ceiling_nano,
        key.max_output_tokens,
        gate,
    )
