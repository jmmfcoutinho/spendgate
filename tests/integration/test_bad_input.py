"""Hostile input is the caller's error (400 `invalid_request`): never a 500, never sent on.

Deeply nested JSON, an estimate that overflows, and admin times that overflow a datetime.
"""

from pathlib import Path

import pytest

from spendgate.core.errors import ErrorCode
from spendgate.pipeline import steps
from spendgate.pipeline.model import CHAT
from tests.integration.harness import async_test, open_stack
from tests.integration.test_admin import BASE, PATH
from tests.integration.test_admin_support import open_admin
from tests.integration.uploads import bearer

OVERFLOW = "0001-01-01T00:00:00+14:00"


@async_test
async def test_sr_07_deeply_nested_json_is_invalid_request_not_a_500(tmp_path: Path) -> None:
    """SR-07: JSON nested past the parser's recursion limit is a 400."""
    async with open_stack(tmp_path) as s:
        deep = b"[" * 200_000 + b"]" * 200_000
        r = await s.client.post(CHAT.path, content=deep, headers=bearer(s))
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"


@async_test
async def test_sr_07_arithmetic_error_in_estimation_is_invalid_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SR-07: a decimal overflow while estimating is a 400 and nothing is sent."""
    import decimal  # noqa: PLC0415

    def overflow(*args: object, **kwargs: object) -> object:
        raise decimal.Overflow("too big")

    monkeypatch.setattr(steps, "estimate", overflow)
    async with open_stack(tmp_path) as s:
        r = await s.chat()
        assert r.status_code == 400 and r.json()["error"]["code"] == ErrorCode.INVALID_REQUEST
        s.assert_no_contact()


@async_test
async def test_sr_07_an_overflowing_time_is_a_400_not_a_500(tmp_path: Path) -> None:
    """SR-07 (admin times): a key expiry or a usage `since` that overflows is a 400."""
    async with open_admin(tmp_path) as a:
        err = await a.error(
            "POST", f"{BASE}/keys", 400,
            json={"scope_id": PATH, "name": "k", "expires_at": OVERFLOW},
        )  # fmt: skip
        assert err["code"] == "invalid_request"
        err = await a.error("GET", f"{BASE}/usage", 400, params={"since": OVERFLOW})
        assert err["code"] == "invalid_request"


@async_test
async def test_sr_07_grant_times_that_overflow_are_a_400(tmp_path: Path) -> None:
    """SR-07 (admin times): grant `valid_from` and `valid_until` that overflow are a 400."""
    async with open_admin(tmp_path) as a:
        made = await a.ok(
            "POST", f"{BASE}/scopes/{PATH}/budgets", 201,
            json={"cap": {"amount": "1", "currency": "USD"}},
        )  # fmt: skip
        grants = f"{BASE}/budgets/{made['id']}/grants"
        money = {"amount": "1", "currency": "USD"}
        for field in ("valid_from", "valid_until"):
            body = {
                "amount": money,
                "valid_from": "2031-01-01T00:00:00Z",
                "valid_until": "2031-02-01T00:00:00Z",
                "reason": "r",
            } | {field: OVERFLOW}
            assert (await a.error("POST", grants, 400, json=body))["code"] == "invalid_request"
