"""Admin commands: each maps arguments to admin API calls and renders the answer.

A command returns `(data, text)`: `--json` prints `data` (the API's own JSON), otherwise `text`.
Nothing here touches a ledger; `--local` runs the same admin app in-process.
"""

import shlex
from argparse import Namespace
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from spendgate.cli.client import EXIT_REFUSED, Client, CliError, Json
from spendgate.cli.render import dash, money, table
from spendgate.core.money import MoneyError, parse_amount
from spendgate.gateway.admin_http import money_json

type Output = tuple[Json, str]
type Command = Callable[[Client, Namespace], Awaitable[Output]]
BASE = "/admin/v1"
_UNIT = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def items(data: Json, key: str = "data") -> list[Json]:
    found = data[key]
    return [r for r in found if isinstance(r, dict)] if isinstance(found, list) else []


def amount(text: str) -> Json:
    """`"50 EUR"` as an API Money object."""
    try:
        parsed = parse_amount(text)
    except MoneyError:
        raise CliError(EXIT_REFUSED, f"{text!r} is not an amount like '50 EUR'") from None
    return dict(money_json(parsed.nano, parsed.currency))


def until(text: str) -> str:
    """`7d` (from now) or an RFC 3339 time, as RFC 3339."""
    if len(text) > 1 and text[:-1].isdecimal() and text[-1] in _UNIT:
        moment = datetime.now(UTC) + timedelta(**{_UNIT[text[-1]]: int(text[:-1])})
        return moment.isoformat(timespec="seconds").replace("+00:00", "Z")
    return text


def _json(a: Namespace) -> bool:
    """`--json` prints the API's data only, so lookups made for the text are skipped."""
    return bool(getattr(a, "json", False))


def _strs(**fields: str | None) -> dict[str, str]:
    return {k: v for k, v in fields.items() if v is not None}


def _compact(**fields: object) -> Json:
    return {k: v for k, v in fields.items() if v is not None}


# --- keys ------------------------------------------------------------------------------------


async def _scope_path(c: Client, scope_id: object) -> object:
    """The scope's path (`org:acme/service:api`), or its id when the lookup fails.

    A key without the read scope can still create keys: the secret must print all the same.
    """
    try:
        return (await c.call("GET", f"{BASE}/scopes/{scope_id}"))["path"]
    except (CliError, KeyError, TypeError, ValueError):
        return scope_id


async def _key_rows(c: Client, keys: list[Json], *, paths: bool = True) -> str:
    """The SCOPE column: each scope's path (one lookup per scope), or the ids if not `paths`."""
    ids = dict.fromkeys(k["scope_id"] for k in keys)
    shown = {s: await _scope_path(c, s) if paths else s for s in ids}
    rows = [
        (k["id"], k["name"], shown[k["scope_id"]], k["prefix"], dash(k["expires_at"]), k["revoked"])
        for k in keys
    ]
    return table(("ID", "NAME", "SCOPE", "PREFIX", "EXPIRES", "REVOKED"), rows)


async def keys_create(c: Client, a: Namespace) -> Output:
    limits = _compact(
        rpm=a.rpm, tpm=a.tpm, max_concurrent=a.max_concurrent, max_output_tokens=a.max_output_tokens
    )
    body = _compact(
        scope_id=a.scope,
        name=a.name,
        expires_at=a.expires,
        allowed_models=a.models.split(",") if a.models else None,
        limits=limits or None,
    )
    made = await c.call("POST", f"{BASE}/keys", body=body)
    secret = f"secret (shown once, store it now): {made['secret']}"
    try:
        rows = await _key_rows(c, [made], paths=not _json(a))
    except Exception:  # the secret exists only here: no display step may lose it
        rows = f"created key {made.get('id')}"
    return made, f"{rows}\n\n{secret}"


async def keys_update(c: Client, a: Namespace) -> Output:
    """PATCH the key; the API replaces the whole limits object, so unchanged limits are re-sent."""
    changes = _compact(
        rpm=a.rpm, tpm=a.tpm, max_concurrent=a.max_concurrent, max_output_tokens=a.max_output_tokens
    )
    models = a.models.split(",") if a.models else None
    if not changes and models is None:
        raise CliError(
            EXIT_REFUSED,
            "nothing to change: give --rpm, --tpm, --max-concurrent, "
            "--max-output-tokens or --models",
        )
    body: Json = {}
    if changes:
        current = await c.call("GET", f"{BASE}/keys/{a.key_id}")
        kept = current.get("limits")
        body["limits"] = {**(kept if isinstance(kept, dict) else {}), **changes}
    if models is not None:
        body["allowed_models"] = models
    done = await c.call("PATCH", f"{BASE}/keys/{a.key_id}", body=body)
    return done, f"updated {done['id']}"


async def keys_list(c: Client, a: Namespace) -> Output:
    query = _strs(scope_id=a.scope, include_revoked="true" if a.include_revoked else None)
    data = await c.call("GET", f"{BASE}/keys", query=query)
    return data, await _key_rows(c, items(data), paths=not _json(a))


async def keys_revoke(c: Client, a: Namespace) -> Output:
    done = await c.call("POST", f"{BASE}/keys/{a.key_id}/revoke")
    return done, f"revoked {done['id']}"


async def keys_rotate(c: Client, a: Namespace) -> Output:
    body = _compact(grace_seconds=a.grace_seconds)
    made = await c.call("POST", f"{BASE}/keys/{a.key_id}/rotate", body=body)
    note = "the old key stays valid for the grace period"
    text = f"new key {made['id']}; {note}\n\nsecret (shown once, store it now): {made['secret']}"
    return made, text


# --- budgets ---------------------------------------------------------------------------------


def _budget_rows(budgets: list[Json]) -> str:
    rows = [
        (
            b["id"],
            b["period"],
            money(b["cap"]),
            money(b["effective_cap"]),
            money(b["spent"]),
            money(b["held"]),
            money(b["remaining"]),
            "REVIEW" if b["review_required_at"] else "",
        )
        for b in budgets
    ]
    return table(("ID", "PERIOD", "CAP", "EFFECTIVE", "SPENT", "HELD", "REMAINING", "FLAG"), rows)


async def budgets_list(c: Client, a: Namespace) -> Output:
    data = await c.call("GET", f"{BASE}/scopes/{a.scope}/budgets")
    return data, _budget_rows(items(data))


async def budgets_set(c: Client, a: Namespace) -> Output:
    """Upsert: set the cap of the scope's budget with this period and filter, or create it."""
    base = f"{BASE}/scopes/{a.scope}/budgets"
    cap = amount(a.cap)
    existing = [
        b
        for b in items(await c.call("GET", base))
        if b["period"] == a.period and b["model_filter"] == a.model_filter
    ]
    if existing:
        body = _compact(cap=cap, soft_pct=a.soft_pct, note=a.note)
        done = await c.call("PATCH", f"{BASE}/budgets/{existing[0]['id']}", body=body)
    else:
        body = _compact(
            period=a.period, cap=cap, soft_pct=a.soft_pct, model_filter=a.model_filter, note=a.note
        )
        done = await c.call("POST", base, body=body)
    return done, _budget_rows([done])


async def budgets_grant(c: Client, a: Namespace) -> Output:
    body = {"amount": amount(a.amount), "valid_until": until(a.until), "reason": a.reason}
    done = await c.call("POST", f"{BASE}/budgets/{a.budget_id}/grants", body=body)
    return done, f"grant {done['id']}: {money(done['amount'])} until {done['valid_until']}"


async def budgets_ack(c: Client, a: Namespace) -> Output:
    done = await c.call(
        "POST", f"{BASE}/budgets/{a.budget_id}/ack-overrun", body={"reason": a.reason}
    )
    return done, _budget_rows([done])


# --- kill switches, usage, status ------------------------------------------------------------


async def kill_on(c: Client, a: Namespace) -> Output:
    kind = next((k for k in ("scope", "key", "provider", "model") if getattr(a, k)), "global")
    body = _compact(target_kind=kind, target_ref=getattr(a, kind, None), reason=a.reason)
    made = await c.call("POST", f"{BASE}/kill-switches", body=body)
    # The same --local/--config/--url the user typed, so the hint works when pasted as is.
    again = getattr(a, "again", "")
    lift = f"spendgate kill off {shlex.quote(str(made['id']))}{again}"
    return made, f"kill switch {made['id']} engaged ({kind}); lift it with: {lift}"


async def kill_off(c: Client, a: Namespace) -> Output:
    await c.call("DELETE", f"{BASE}/kill-switches/{a.switch_id}")
    return {"lifted": a.switch_id}, f"lifted {a.switch_id}"


async def usage(c: Client, a: Namespace) -> Output:
    query = _strs(
        by=a.by,
        since=a.since,
        until=a.until,
        scope_id=a.scope,
        model=a.model,
        tag=a.tag,
        basis=a.basis,
        limit=str(a.limit) if a.limit else None,
    )
    data = await c.call("GET", f"{BASE}/usage", query=query)
    dims = list(dict.fromkeys(k for r in items(data, "rows") for k in _group(r)))
    rows = [
        [
            *(_group(r).get(d, "") for d in dims),
            r["calls"],
            r["tokens_in"],
            r["tokens_out"],
            money(r["cost"]),
        ]
        for r in items(data, "rows")
    ]
    text = table([d.upper() for d in dims] + ["CALLS", "TOKENS_IN", "TOKENS_OUT", "COST"], rows)
    return data, f"{text}\n\ntotal {money(data['total'])} ({data['basis']})"


def _group(row: Json) -> dict[str, object]:
    group = row["group"]
    return dict(group) if isinstance(group, dict) else {}


async def status(c: Client, a: Namespace) -> Output:
    data = await c.call("GET", f"{BASE}/status")
    ledger = data["ledger"] if isinstance(data["ledger"], dict) else {}
    lines = [
        f"spendgate {data['version']}, up {data.get('uptime_seconds', 0)}s",
        f"ledger: {ledger.get('backend')} healthy={ledger.get('healthy')} "
        f"pending_holds={ledger.get('pending_holds', '?')}",
    ]
    switches = items(data, "kill_switches")
    lines.append(
        f"kill switches: {len(switches)}"
        + "".join(
            f"\n  {k['id']}  {k['target_kind']} {k.get('target_ref', '')}  {k['reason']}"
            for k in switches
        )
    )
    alerts = items(data, "budget_alerts")
    lines.append(
        f"budget alerts: {len(alerts)}"
        + "".join(f"\n  {x['kind']}  scope {x['scope_id']}" for x in alerts)
    )
    return data, "\n".join(lines)


COMMANDS: dict[str, Command] = {
    "keys create": keys_create, "keys list": keys_list, "keys update": keys_update,
    "keys revoke": keys_revoke,
    "keys rotate": keys_rotate, "budgets set": budgets_set, "budgets list": budgets_list,
    "budgets grant": budgets_grant, "budgets ack-overrun": budgets_ack, "kill on": kill_on,
    "kill off": kill_off, "usage": usage, "status": status,
}  # fmt: skip
