"""Budget warnings through the gateway: one `spendgate.budget` JSON line per transition.

The soft threshold (once per period), a cap breach (once per crossing) and the block that follows
are never logged again on later calls, and carry budget facts only: no prompt, key, user or
session data.
"""

import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from spendgate.core.budgets import Period
from spendgate.core.money import Money
from spendgate.core.sessions import SessionDefaults
from spendgate.gateway.logs import JsonFormatter
from spendgate.pipeline.alerts import BUDGET_LOGGER
from tests.fakes import ABSURD_USAGE, FakeUpstream, Script
from tests.integration.harness import NOW_MS, Stack, async_test, open_stack, usd

PROMPT = "please-keep-this-prompt-out-of-logs"
SESSION = "caller-session-not-for-logs"
USER_LABEL = "user-label-not-for-logs"
FIELDS = {
    "event", "logger", "budget_id", "scope_path", "period", "spent", "cap", "currency",
    "level", "ts", "scope_kind",
}  # fmt: skip


@contextmanager
def budget_log() -> Iterator[io.StringIO]:
    """The JSON lines the gateway would write for `spendgate.budget`."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(BUDGET_LOGGER)
    saved = logger.handlers, logger.propagate, logger.level
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.INFO)
    try:
        yield buf
    finally:
        logger.handlers, logger.propagate = saved[0], saved[1]
        logger.setLevel(saved[2])


def lines(buf: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buf.getvalue().splitlines()]


async def call(s: Stack, session: str | None = None) -> int:
    body = {"model": "alpha", "max_tokens": 100, "user": USER_LABEL}
    body["messages"] = [{"role": "user", "content": PROMPT}]
    headers = {"x-spendgate-user": USER_LABEL}
    if session is not None:
        headers["x-spendgate-session"] = session
    r = await s.post("/v1/chat/completions", body, headers=headers)
    return r.status_code


def assert_budget_facts_only(s: Stack, buf: io.StringIO) -> None:
    text = buf.getvalue()
    key_ids = {e.key_id for e in s.observer.settled}
    for secret in (PROMPT, USER_LABEL, SESSION, s.secret, s.secret[:8], "sg_req", *key_ids):
        assert secret not in text
    for line in lines(buf):
        assert set(line) <= FIELDS | {"period_start"}, line
        assert line["level"] == "WARNING" and line["logger"] == BUDGET_LOGGER


@async_test
async def test_soft_threshold_is_logged_once_per_budget_period(tmp_path: Path) -> None:
    async with open_stack(tmp_path, budget="100") as s:
        with budget_log() as buf:
            assert await call(s) == 200
            hold = s.observer.held[0].hold.nano
            # A fresh budget the next hold alone takes to its 50 % threshold.
            org = s.scope.parent_id
            assert org is not None
            budget = await s.ledger.create_budget(
                org, Period.MONTH, 2 * hold, now=NOW_MS, soft_pct=50
            )
            assert buf.getvalue() == ""
            for _ in range(3):
                assert await call(s) == 200
        (line,) = lines(buf)
        org_row = await s.ledger.get_scope(org)
        assert org_row is not None
        assert line["event"] == "soft_threshold" and line["budget_id"] == budget.id
        assert (line["scope_path"], line["period"]) == (org_row.path, "month")
        assert line["period_start"] == "2026-11-01T00:00:00Z"
        assert line["spent"] == Money(hold, "USD").format().split()[0]
        assert line["cap"] == Money(2 * hold, "USD").format().split()[0]
        assert line["currency"] == "USD"
        assert_budget_facts_only(s, buf)


@async_test
async def test_a_breach_overrun_logs_cap_breached_and_blocked_once(tmp_path: Path) -> None:
    fake = FakeUpstream()
    fake.enqueue(Script(usage=ABSURD_USAGE))  # costs far more than the hold: a breach overrun
    async with open_stack(tmp_path, fake=fake, budget="1") as s:
        with budget_log() as buf:
            assert await call(s) == 200
            assert await call(s) == 402  # blocked until ack-overrun: refused, nothing new logged
            assert await call(s) == 402
        events = [(line["event"], line["budget_id"]) for line in lines(buf)]
        assert events == [("cap_breached", s.budget.id), ("blocked", s.budget.id)]
        assert {line["scope_path"] for line in lines(buf)} == {s.scope.path}
        assert all(line["period"] == "none" and "period_start" not in line for line in lines(buf))
        assert_budget_facts_only(s, buf)


@async_test
async def test_a_session_budget_warning_names_the_key_scope_not_the_session(
    tmp_path: Path,
) -> None:
    fake = FakeUpstream()
    fake.enqueue(Script(usage=ABSURD_USAGE))  # breaches the session budget and the key's
    defaults = SessionDefaults(budget_nano=usd("1"))
    async with open_stack(tmp_path, fake=fake, budget="1", session_defaults=defaults) as s:
        with budget_log() as buf:
            assert await call(s, SESSION) == 200
        session_lines = [line for line in lines(buf) if line.get("scope_kind") == "session"]
        assert [line["event"] for line in session_lines] == ["cap_breached", "blocked"]
        assert {line["scope_path"] for line in session_lines} == {s.scope.path}
        assert s.observer.settled and s.observer.settled[0].key_id
        assert_budget_facts_only(s, buf)
