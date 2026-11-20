"""Admin API regressions found by the fuzz targets (see test_regression_allowlists.py)."""

from pathlib import Path

import pytest

from spendgate.gateway.admin_http import ApiError, parse_time
from tests.fakes import FakeUpstream
from tests.integration.harness import async_test
from tests.integration.test_admin_support import open_admin


@pytest.mark.parametrize("path", ["/admin/v1/scopes/a%0Ab", "/admin/%0A0", "/admin/a%0Ab"])
@async_test
async def test_fz02_newline_in_admin_path_gets_the_error_shape(tmp_path: Path, path: str) -> None:
    async with open_admin(tmp_path, fake=FakeUpstream()) as a:
        response = await a.call("GET", path)
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["error"]["code"] == "not_found"


# FZ-03 (found at HEAD aec93ec, fixed in the working tree by the concurrent verification work):
# parse_time converted an aware time to UTC with `astimezone`, which raises OverflowError when the
# result leaves years 1..9999, so ?since=0001-01-01T00:00:00+14:00 was a 500. Pinned here.
@pytest.mark.parametrize(
    "raw", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00", "0001-01-01000+0001"]
)
def test_fz03_parse_time_out_of_range_is_an_api_error(raw: str) -> None:
    with pytest.raises(ApiError) as caught:
        parse_time(raw, 1_800_000_000_000, "since")
    assert caught.value.refusal.param == "since"


@async_test
async def test_fz03_out_of_range_since_is_a_400_over_http(tmp_path: Path) -> None:
    async with open_admin(tmp_path, fake=FakeUpstream()) as a:
        response = await a.call(
            "GET", "/admin/v1/usage", params={"since": "0001-01-01T00:00:00+14:00"}
        )
        assert response.status_code == 400


@async_test
async def test_fz02_newline_in_data_plane_path_gets_the_error_shape(tmp_path: Path) -> None:
    from tests.integration.harness import open_stack  # noqa: PLC0415

    async with open_stack(tmp_path) as s:
        for path in ("/v1/a%0Ab", "/v1/models/a%0Ab", "/v1/%0A"):
            response = await s.client.get(path, headers={"Authorization": f"Bearer {s.secret}"})
            assert response.status_code == 404, path
            assert response.json()["error"]["code"] == "not_found"
        outside = await s.client.get("/elsewhere%0Ax")
        assert outside.status_code == 404  # outside /v1 keeps Starlette's plain answer
