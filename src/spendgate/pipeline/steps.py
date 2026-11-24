"""Steps 3 to 7 as small functions: validate, route, price, admit limits, build the hold.

Each returns its result or a `Refusal` (data, never raised); no step contacts a provider.
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

from spendgate.config import Route, Settings
from spendgate.core.catalog import ModelNotPriced, PriceEntry
from spendgate.core.cost import UnpricedUsage, worst_case
from spendgate.core.errors import ErrorCode, LimitName, Refusal
from spendgate.core.estimate import RequestKind, estimate, resolve_output_ceiling
from spendgate.core.limits import Admit, Deny, LimitKey, LimitStore
from spendgate.core.policy import (
    SESSION_HEADER,
    TAGS_HEADER,
    USER_HEADER,
    KeyPolicy,
    Labels,
    Limits,
    Plan,
    model_allowed,
    resolve,
)
from spendgate.ledger.port import Candidate, HoldMeta, HoldRequest
from spendgate.pipeline.faults import InternalEvent, fault_of
from spendgate.pipeline.model import Inbound, Observer, report_internal
from spendgate.pipeline.routing import Target
from spendgate.pipeline.settle import Pricing, fx_rate_nano
from spendgate.providers.port import CleanRequest, Json, Provider

_USER_VALUE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_MINUTE_MS = 60_000
_MS = 1000
# Output-ceiling fields per kind; the first is the one field the forwarded body carries.
_CEILING = {
    RequestKind.CHAT: ("max_completion_tokens", "max_tokens"),
    RequestKind.RESPONSES: ("max_output_tokens",),
    RequestKind.MESSAGES: ("max_tokens",),
}
_STREAMING = frozenset({RequestKind.CHAT, RequestKind.RESPONSES, RequestKind.MESSAGES})
_SECRET_HEADERS = frozenset({"authorization", "x-api-key"})


@dataclass(frozen=True, slots=True)
class Prepared:
    """What steps 3 to 5 decided about one request."""

    route: Route
    provider: Provider
    request: CleanRequest
    pricing: Pricing
    labels: Labels
    user_label: str | None
    tags: Mapping[str, str]
    tokens: int  # estimated input plus output ceiling, for the TPM limit
    limits: Limits
    free_statuses: frozenset[int]


def find_route(routes: Mapping[str, Route], name: str) -> Route | None:
    """An alias, or a catalog id (the first alias that serves it, in config order)."""
    if name in routes:
        return routes[name]
    return next((r for r in routes.values() if r.catalog_id == name), None)


def labels_of(
    headers: Mapping[str, str], body: Mapping[str, Json]
) -> tuple[Labels, str | None, dict[str, str]]:
    """Labels from the X-Spendgate-* headers; the user falls back to the body (attribution only).

    A bad header is left for `check_labels` (inside `resolve`) to refuse; a bad body fallback
    value is dropped, because it is the caller's own field and never a spendgate contract.
    """
    tags_raw = headers.get(TAGS_HEADER.lower())
    tags = tuple(t.strip() for t in tags_raw.split(",")) if tags_raw is not None else ()
    user = headers.get(USER_HEADER.lower())
    labels = Labels(headers.get(SESSION_HEADER.lower()), user, tags)
    attributed = user if user is not None else _body_user(body)
    return labels, attributed, {t: "1" for t in tags}


def _body_user(body: Mapping[str, Json]) -> str | None:
    value = body.get("user")
    meta = body.get("metadata")
    if value is None and isinstance(meta, Mapping):
        value = meta.get("user_id")
    if isinstance(value, str) and _USER_VALUE.fullmatch(value):
        return value
    return None


def _set_ceiling(kind: RequestKind, body: dict[str, Json], ceiling: int) -> int | None:
    """One ceiling field, equal to the resolved one: injected when absent, the smaller kept when
    the client sent two (a provider that honours the larger would bill past the hold, M-15)."""
    names = _CEILING.get(kind)
    if not names:
        return None
    for name in names:
        body.pop(name, None)
    body[names[0]] = ceiling
    return ceiling


def _check_shape(inbound: Inbound, settings: Settings) -> Refusal | None:
    """Pipeline-level allowlist rules on top of the adapter's (protocol 2.4 and 2.8)."""
    body = inbound.body
    if body.get("stream") is True and inbound.endpoint.kind not in _STREAMING:
        return Refusal(ErrorCode.INVALID_REQUEST, "This endpoint does not stream.", "stream")
    items = body.get("input")
    cap = settings.config.defaults.max_embedding_items
    too_many = isinstance(items, list) and len(items) > cap
    if inbound.endpoint.kind is RequestKind.EMBEDDING and too_many:
        return Refusal(ErrorCode.INVALID_REQUEST, f"At most {cap} inputs are allowed.", "input")
    return None


@dataclass(frozen=True, slots=True)
class _Routed:
    route: Route
    name: str  # what the client asked for
    labels: Labels
    user_label: str | None
    tags: dict[str, str]
    plan: Plan


def _route(settings: Settings, inbound: Inbound, key: KeyPolicy, now: int) -> _Routed | Refusal:
    """Step 3 and 4 (model side): who may use what, labels, kill switches, session header."""
    name = inbound.body.get("model")
    if not isinstance(name, str) or not name:
        return Refusal(ErrorCode.INVALID_REQUEST, "model is required.", "model")
    route = find_route(settings.routes, name)
    if route is None or not (model_allowed(key, name) or model_allowed(key, route.catalog_id)):
        return Refusal(ErrorCode.MODEL_NOT_ALLOWED, f"Model {name} is not allowed for this key.")
    labels, user_label, tags = labels_of(inbound.headers, inbound.body)
    # Budgets and the session row come from the ledger inside the hold; this is only the cache.
    plan = resolve(
        replace(key, allowed_models=None), labels, route.catalog_id, now,
        budgets=(), chain_scope_ids=(), providers=(route.catalog_id.partition("/")[0],),
    )  # fmt: skip
    if isinstance(plan, Refusal):
        return plan
    return _Routed(route, name, labels, user_label, tags, plan)


@dataclass(frozen=True, slots=True)
class Context:
    """The inputs of steps 3 to 5 for one request."""

    settings: Settings
    providers: Mapping[str, Provider]
    inbound: Inbound
    key: KeyPolicy
    request_id: str
    now: int
    observer: Observer | None = None  # told about a surprise the catch-all below turns away


def prepare(ctx: Context) -> Prepared | Refusal:
    """Steps 3 to 5: allowlist, routing, pricing, output ceiling, worst case, labels, limits.

    A body that drives the interpreter out of recursion or a number out of range is the caller's
    fault: `invalid_request`, not a 500 (SR-07).
    """
    try:
        return _prepare(ctx)
    except (RecursionError, ArithmeticError):
        return Refusal(ErrorCode.INVALID_REQUEST, "The request body could not be processed.")
    except Exception as exc:  # defence in depth: nothing is held yet, so refuse rather than 500
        report_internal(ctx.observer, InternalEvent(ctx.request_id, fault_of("steps.prepare", exc)))
        return Refusal(ErrorCode.INVALID_REQUEST, "The request body could not be processed.")


def _prepare(ctx: Context) -> Prepared | Refusal:
    routed = _route(ctx.settings, ctx.inbound, ctx.key, ctx.now)
    if isinstance(routed, Refusal):
        return routed
    if (bad := _check_shape(ctx.inbound, ctx.settings)) is not None:
        return bad
    slug, _, model_part = routed.route.catalog_id.partition("/")
    try:
        entry = ctx.settings.catalog.lookup(slug, model_part, ctx.now)
    except ModelNotPriced as exc:
        return exc.refusal()
    key_cap = ctx.key.max_output_tokens or ctx.settings.config.defaults.max_output_tokens
    clean = _clean_request(ctx, routed, entry, key_cap)
    if isinstance(clean, Refusal):
        return clean
    if (refusal := ctx.providers[slug].validate(clean)) is not None:
        return refusal
    return _price(ctx, routed, entry, clean, key_cap)


def _clean_request(
    ctx: Context, routed: _Routed, entry: PriceEntry, key_cap: int | None
) -> CleanRequest | Refusal:
    """The validated request: output ceiling resolved once and set, secrets kept out."""
    kind = ctx.inbound.endpoint.kind
    body: dict[str, Json] = dict(ctx.inbound.body)
    ceiling = resolve_output_ceiling(kind, body, entry=entry, key_max_output_tokens=key_cap)
    if isinstance(ceiling, Refusal):
        return ceiling
    held = _set_ceiling(kind, body, ceiling)
    return CleanRequest(
        ctx.inbound.endpoint.path, routed.name, body, body.get("stream") is True,
        {k: v for k, v in ctx.inbound.headers.items() if k not in _SECRET_HEADERS},
        ctx.inbound.files, "chatcmpl-" + ctx.request_id.removeprefix("sg_req_"),
        ctx.now // _MS, held,
    )  # fmt: skip


def _price(
    ctx: Context, routed: _Routed, entry: PriceEntry, clean: CleanRequest, key_cap: int | None
) -> Prepared | Refusal:
    """Worst case over the candidates (one price for the whole alias), then the per-call cap."""
    settings, route = ctx.settings, routed.route
    estimated = estimate(
        ctx.inbound.endpoint.kind,
        _estimate_body(ctx.inbound, clean.body),
        entry=entry,
        key_max_output_tokens=key_cap,
        output_ceiling=clean.output_ceiling,  # the value the adapter forwards, not re-read
    )
    if isinstance(estimated, Refusal):
        return estimated
    try:
        native = worst_case(entry, estimated)
    except UnpricedUsage:
        return Refusal(ErrorCode.MODEL_NOT_PRICED, f"{route.catalog_id} cannot price this request.")
    worst = settings.catalog.in_ledger(native)
    cap = ctx.key.per_call_ceiling_nano
    if cap is not None and worst.nano > cap:
        return Refusal(
            ErrorCode.REQUEST_TOO_LARGE, "The worst-case cost is above this key's per-call ceiling."
        )
    pricing = Pricing(entry, fx_rate_nano(settings.catalog, entry.currency), worst)
    tokens = estimated.input_tokens + estimated.max_output_tokens + estimated.audio_input_tokens
    return Prepared(
        route, ctx.providers[route.catalog_id.partition("/")[0]], clean, pricing, routed.labels,
        routed.user_label, routed.tags, max(tokens, 1), _limits(routed.plan.limits, settings),
        settings.catalog.free_statuses(route.catalog_id.partition("/")[0]),
    )  # fmt: skip


def _estimate_body(inbound: Inbound, body: Mapping[str, Json]) -> Mapping[str, object]:
    """Transcriptions are estimated from the file bytes plus the form fields."""
    if inbound.files:
        return {**body, "file": inbound.files.get("file", b"")}
    return body


def _limits(key: Limits, settings: Settings) -> Limits:
    """The key's own limits, else the configured defaults."""
    d = settings.config.defaults.limits
    return Limits(key.rpm or d.rpm, key.tpm or d.tpm, key.max_concurrent or d.concurrency)


# --- step 6: limits ---------------------------------------------------------------------------


def _limit_keys(key_id: str, limits: Limits, tokens: int) -> tuple[list[LimitKey], list[int]]:
    keys: list[LimitKey] = []
    amounts: list[int] = []
    for name, limit, window, amount in (
        (LimitName.RPM, limits.rpm, _MINUTE_MS, 1),
        (LimitName.TPM, limits.tpm, _MINUTE_MS, tokens),
        (LimitName.CONCURRENCY, limits.max_concurrent, None, 1),
    ):
        if limit is not None:
            keys.append(LimitKey(f"{name.value}:{key_id}", limit, window))
            amounts.append(amount)
    return keys, amounts


def tpm_key(key_id: str) -> str:
    return f"{LimitName.TPM.value}:{key_id}"


async def admit_limits(
    store: LimitStore, key_id: str, limits: Limits, tokens: int, now: int
) -> Admit | Refusal | None:
    """Admit RPM, TPM and concurrency together or refuse; None when the key has no limits."""
    keys, amounts = _limit_keys(key_id, limits, tokens)
    if not keys:
        return None
    result = await store.try_acquire(keys, amounts, now)
    if isinstance(result, Admit):
        return result
    return _denied(result)


async def admit_intake(
    store: LimitStore, key: KeyPolicy, settings: Settings, now: int
) -> Admit | Refusal | None:
    """Before the body is read: one in-flight upload per unit of the key's concurrency limit.

    Released as soon as the body is read; the real admission (`admit_limits`) follows the parse.
    """
    limit = _limits(Limits(key.rpm, key.tpm, key.max_concurrent), settings).max_concurrent
    if limit is None:
        return None
    name = LimitKey(f"{LimitName.CONCURRENCY.value}:intake:{key.id}", limit, None)
    result = await store.try_acquire([name], [1], now)
    return result if isinstance(result, Admit) else _denied(result)


def _denied(deny: Deny) -> Refusal:
    which = deny.key.partition(":")[0]
    if deny.permanent:  # the request alone is bigger than the limit: retrying cannot help
        return Refusal(
            ErrorCode.REQUEST_TOO_LARGE,
            f"The request alone exceeds this key's {which} limit.",
            details={"limit": which},
        )
    return Refusal(
        ErrorCode.RATE_LIMITED,
        f"This key's {which} limit is reached.",
        details={"limit": which},
        retry_after_seconds=max(math.ceil(deny.retry_after_ms / _MS), 1),
    )


# --- step 7: the hold request ------------------------------------------------------------------


def hold_request(  # noqa: PLR0913, PLR0917 - the facts the admission transaction needs
    settings: Settings, prepared: Prepared, targets: tuple[Target, ...], key: KeyPolicy,
    endpoint: str, request_id: str, instance: str, now: int,
) -> HoldRequest:  # fmt: skip
    """One hold for the request: worst = the same price for every candidate of the catalog id."""
    pricing = prepared.pricing
    meta = HoldMeta(
        endpoint,
        prepared.route.catalog_id,
        settings.catalog_sha256,
        tuple(
            Candidate(t.provider, t.label, pricing.entry.price_id, pricing.entry.currency,
                      pricing.fx_rate_nano)
            for t in targets
        ),
        prepared.user_label,
        prepared.tags,
    )  # fmt: skip
    return HoldRequest(
        request_id, instance, now, settings.lease_ms, pricing.worst.nano, key,
        prepared.route.catalog_id, tuple(sorted({t.provider for t in targets})), meta,
        prepared.labels.session,
    )  # fmt: skip
