"""`/readyz` on the data port is status only; details and the catalog-age signals live behind
the admin key (SR-06, SR-05)."""

from pathlib import Path

import pytest

from spendgate.gateway.observability import STALE_CATALOG_DAYS
from spendgate.gateway.wiring import bind_catalog_metrics, catalog_age_seconds
from tests.integration.harness import NOW_MS, async_test
from tests.integration.test_admin_support import open_admin

DAY_S = 86_400


@async_test
async def test_t_obs_03_data_port_readyz_names_no_reason_warning_or_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_admin(tmp_path) as a:
        ok = await a.stack.client.get("/readyz")
        assert (ok.status_code, ok.json()) == (200, {"status": "ready"})
        a.stack.clock.set(NOW_MS + 400 * DAY_S * 1000)  # stale catalog and clock skew: warnings
        await a.ok(
            "POST", "/admin/v1/kill-switches", 201,
            json={"target_kind": "global", "reason": "drill"},
        )  # fmt: skip
        warned = await a.stack.client.get("/readyz")  # a kill switch is a warning (ADR-0015)
        assert (warned.status_code, warned.json()) == (200, {"status": "ready"})

        async def unreachable(_scope_id: str) -> None:
            raise ConnectionError("postgresql://db.invalid/sg")

        monkeypatch.setattr(a.stack.ledger, "get_scope", unreachable)
        down = await a.stack.client.get("/readyz")
        assert (down.status_code, down.json()) == (503, {"status": "not_ready"})
        for word in ("kill", "skew", "catalog", "ledger", "postgresql"):
            assert word not in warned.text and word not in down.text


@async_test
async def test_t_obs_03_readiness_detail_needs_usage_read(tmp_path: Path) -> None:
    async with open_admin(tmp_path) as a:
        assert (await a.call("GET", "/admin/v1/readiness", key="")).status_code == 401
        assert (await a.call("GET", "/admin/v1/readiness")).status_code == 200


@async_test
async def test_t_cat_03_a_stale_catalog_warns_on_readiness_and_ages_the_metric(
    tmp_path: Path,
) -> None:
    async with open_admin(tmp_path) as a:
        fresh = await a.ok("GET", "/admin/v1/readiness")
        assert not any(w.startswith("catalog_stale") for w in fresh["warnings"])  # type: ignore[attr-defined]
        metrics = a.ctx.metrics
        assert metrics is not None
        bind_catalog_metrics(metrics, a.stack.settings, a.stack.clock.now)
        sha = a.stack.settings.catalog_sha256
        assert f'spendgate_catalog_info{{sha="{sha}"}} 1\n' in metrics.render()

        a.stack.clock.advance(seconds=(STALE_CATALOG_DAYS + 5) * DAY_S)
        stale = await a.call("GET", "/admin/v1/readiness")
        assert stale.status_code == 200  # a warning, never a failure
        assert any(w.startswith("catalog_stale") for w in stale.json()["warnings"])
        age = catalog_age_seconds(a.stack.settings, a.stack.clock.now())
        assert age is not None and age > STALE_CATALOG_DAYS * DAY_S
        # the age gauge refreshes on every scrape, with no restart
        assert f"spendgate_catalog_age_seconds {int(age)}\n" in metrics.render()
