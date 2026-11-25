"""Attribution headers are labels, never authority (C-19, TH-10).

`X-Spendgate-User`, `X-Spendgate-Tags` and `X-Spendgate-Session` (and the body's `user` fallback)
only label a call: money always binds to the key's scope. Values are validated (charset, length,
count) before anything is held, and they never reach a log line or a metric label.
"""

import io
import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from spendgate.core.budgets import Period, ScopeKind
from spendgate.core.policy import SESSION_HEADER, TAGS_HEADER, USER_HEADER
from spendgate.core.sessions import SessionDefaults
from spendgate.gateway.logs import configure_json_logging
from spendgate.gateway.metrics import Metrics
from spendgate.gateway.wiring import GatewayObserver
from spendgate.ledger.key_types import KeySpec, Principal
from spendgate.ledger.port import ScopeRow
from tests.integration.harness import CHAT, NOW_MS, Stack, async_test, open_stack, usd

MARK = "fake-attr-4711"  # valid in every label charset, so only the code under test can drop it


async def other_tenant(s: Stack) -> ScopeRow:
    """A second user scope under the same org, with its own budget and key."""
    assert s.scope.parent_id is not None
    other = await s.ledger.create_scope(ScopeKind.USER, "u2", s.scope.parent_id, now=NOW_MS)
    await s.ledger.create_budget(other.id, Period.NONE, usd("10"), now=NOW_MS)
    await s.keys.create_key(KeySpec(other.id, "other-key"), actor="test", now=NOW_MS)
    return other


async def callers_key_id(s: Stack) -> str:
    who = await s.keys.verify_api(s.secret, NOW_MS)
    assert isinstance(who, Principal)
    return who.key_id


async def other_totals(s: Stack, scope: ScopeRow) -> tuple[int, int]:
    (row,) = await s.ledger.totals(scope.id, NOW_MS)
    return row.spent_nano, row.held_nano


def spoofing(other: ScopeRow) -> dict[str, str]:
    """Every attribution header, naming the other tenant every way a caller could."""
    return {USER_HEADER: "u2", TAGS_HEADER: f"u2,{other.id.lower()},acme"}


def body(user: object = "u2") -> dict[str, object]:
    return {
        "model": "alpha", "max_tokens": 100, "user": user,
        "messages": [{"role": "user", "content": "hi"}],
    }  # fmt: skip


def stored_labels(tmp_path: Path, request_id: str) -> list[tuple[str, str, str | None, str | None]]:
    """(key_id, scope_id, user_label, labels) of each attempt, from the ledger file itself."""
    with closing(sqlite3.connect(tmp_path / "ledger.db")) as db:
        rows = db.execute(
            "SELECT key_id, scope_id, user_label, labels FROM calls WHERE request_id = ?",
            (request_id,),
        ).fetchall()
    return [(str(k), str(s), u, lab) for k, s, u, lab in rows]


@async_test
async def test_t_attr_01_headers_naming_another_tenant_charge_only_the_callers_key(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path) as s:
        other = await other_tenant(s)
        r = await s.post(CHAT, body(), headers=spoofing(other))
        assert r.status_code == 200, r.text
        spent, held = await s.totals()
        assert spent > 0 and held == 0  # the caller's own budget paid
        assert await other_totals(s, other) == (0, 0)  # the named tenant was never touched
        rid = r.headers["x-spendgate-request-id"]
        ((key_id, scope_id, user, _),) = stored_labels(tmp_path, rid)
        assert (key_id, scope_id) == (await callers_key_id(s), s.scope.id)
        assert user == "u2"  # kept as the label it is, nothing more


@async_test
async def test_t_attr_01_headers_naming_a_tenant_with_room_do_not_lift_an_exhausted_budget(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path, budget="0") as s:
        other = await other_tenant(s)
        r = await s.post(CHAT, body(), headers=spoofing(other))
        assert r.status_code == 402 and r.json()["error"]["code"] == "budget_exhausted"
        s.assert_no_contact()
        assert await other_totals(s, other) == (0, 0)


HOSTILE = [
    pytest.param({USER_HEADER: "tab\there"}, USER_HEADER, id="user-control-char"),
    pytest.param({USER_HEADER: "esc\x1b[31m"}, USER_HEADER, id="user-escape-sequence"),
    pytest.param({USER_HEADER: "del\x7f"}, USER_HEADER, id="user-del"),
    pytest.param({USER_HEADER: 'q"}{"event":"forged'}, USER_HEADER, id="user-json-breakout"),
    pytest.param({USER_HEADER: "u" * 129}, USER_HEADER, id="user-too-long"),
    pytest.param({TAGS_HEADER: "ok,bad\ttag"}, TAGS_HEADER, id="tag-control-char"),
    pytest.param({TAGS_HEADER: "t" * 33}, TAGS_HEADER, id="tag-too-long"),
    pytest.param({TAGS_HEADER: ",".join(f"t{i}" for i in range(11))}, TAGS_HEADER, id="11-tags"),
    pytest.param({TAGS_HEADER: 'a,b"}'}, TAGS_HEADER, id="tag-json-breakout"),
    pytest.param({SESSION_HEADER: "s\x00"}, SESSION_HEADER, id="session-nul"),
    pytest.param({SESSION_HEADER: "s" * 129}, SESSION_HEADER, id="session-too-long"),
]


@pytest.mark.parametrize(("headers", "param"), HOSTILE)
@async_test
async def test_t_attr_02_hostile_label_values_are_refused_before_any_hold(
    tmp_path: Path, headers: dict[str, str], param: str
) -> None:
    sessions = SessionDefaults(max_steps=10)  # so a session header is not refused for that alone
    async with open_stack(tmp_path, session_defaults=sessions) as s:
        r = await s.post(CHAT, body(), headers=headers)
        assert r.status_code == 400, r.text
        error = r.json()["error"]
        assert (error["code"], error["param"]) == ("invalid_request", param)
        assert error["message"] == f"Invalid value for {param}."  # named, never echoed
        s.assert_no_contact()
        assert await s.totals() == (0, 0)


@async_test
async def test_t_attr_03_label_values_never_reach_a_log_line_or_a_metric(tmp_path: Path) -> None:
    buf = io.StringIO()
    metrics = Metrics()
    observer = GatewayObserver(metrics, configure_json_logging(logging.StreamHandler(buf)))
    sessions = SessionDefaults(max_steps=10)
    valid = {USER_HEADER: f"user-{MARK}", TAGS_HEADER: f"tag-{MARK}", SESSION_HEADER: MARK}
    async with open_stack(
        tmp_path, session_defaults=sessions, pipeline_kwargs={"observer": observer}
    ) as s:
        ok = await s.post(CHAT, body(f"body-{MARK}"), headers=valid)
        injected = await s.post(CHAT, body(), headers={USER_HEADER: f'{MARK}"}}{{"event":"x'})
        dropped = await s.post(CHAT, body(f"{MARK}\n{{}}"))  # a bad body fallback is dropped
    assert ok.status_code == 200, ok.text
    assert injected.status_code == 400
    assert dropped.status_code == 200, dropped.text
    log = buf.getvalue()
    records = [json.loads(line) for line in log.splitlines()]
    assert [r["event"] for r in records] == ["call", "call"]  # one line per call, none forged
    assert MARK not in log
    assert MARK not in metrics.render()
    rid = dropped.headers["x-spendgate-request-id"]
    ((_, _, user, _),) = stored_labels(tmp_path, rid)
    assert user is None  # the ledger keeps no unvalidated label either
