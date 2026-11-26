"""A Region-scoped catalog entry prices a call end to end, and the call row says so (ADR-0028).

The deployment's `region` picks the entry; the hold and the charge use it; the row's price id
(and the hold's candidate, which a crashed request's row is written from) carries `~<region>`.
A deployment elsewhere is priced by the region-less entry, with no suffix.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from tests.fakes import FakeUsage, Script
from tests.integration.harness import MODELS, async_test, open_stack

ID = "openai/gpt-regional-test"
REGIONAL = f"""\
  - id: {ID}
    mode: chat
    currency: USD
    effective_from: "2026-11-01"
    source_url: https://example.test/pricing
    verified: true
    limits: {{max_output_tokens: 1000}}
    meters: {{input_per_mtok: "1", output_per_mtok: "2"}}
  - id: {ID}
    mode: chat
    currency: USD
    effective_from: "2026-11-01"
    regions: [swedencentral]
    source_url: https://example.test/pricing
    verified: true
    limits: {{max_output_tokens: 1000}}
    meters: {{input_per_mtok: "3", output_per_mtok: "6"}}
"""
ROUTES = f"""{MODELS}\
  sweden:
    catalog_id: {ID}
    deployments: [{{provider: openai, base_url: "https://a.upstream.test", region: swedencentral}}]
  elsewhere:
    catalog_id: {ID}
    deployments: [{{provider: openai, base_url: "https://b.upstream.test", region: eastus}}]
"""
USAGE = FakeUsage(input_tokens=10, output_tokens=20)


def recorded(tmp_path: Path, request_id: str) -> tuple[list[str], list[str]]:
    """(calls.price_id per attempt, the hold's candidate price ids) as the ledger stored them."""
    with closing(sqlite3.connect(tmp_path / "ledger.db")) as db:
        rows = db.execute(
            "SELECT price_id FROM calls WHERE request_id = ? ORDER BY attempt", (request_id,)
        ).fetchall()
        (meta,) = db.execute("SELECT meta FROM reservations WHERE id = ?", (request_id,)).fetchone()
    return [r[0] for r in rows], [c["price_id"] for c in json.loads(meta)["candidates"]]


@async_test
async def test_a_region_scoped_entry_prices_the_call_and_names_its_region(tmp_path: Path) -> None:
    async with open_stack(tmp_path, models=ROUTES, catalog_override=REGIONAL) as s:
        s.fake.enqueue(Script(usage=USAGE))
        sweden = await s.chat("sweden")
        s.fake.enqueue(Script(usage=USAGE))
        elsewhere = await s.chat("elsewhere")
        await s.pipeline.aclose()
        assert sweden.status_code == elsewhere.status_code == 200
        scoped = f"{ID}@2026-11-01~swedencentral"
        assert recorded(tmp_path, sweden.headers["x-spendgate-request-id"]) == ([scoped], [scoped])
        plain = f"{ID}@2026-11-01"
        assert recorded(tmp_path, elsewhere.headers["x-spendgate-request-id"]) == ([plain], [plain])
        # 10 in x 3 + 20 out x 6 per 1M, against 10 x 1 + 20 x 2: the Swedish meters were used.
        assert sweden.headers["x-spendgate-cost"] == "0.000150000 USD"
        assert elsewhere.headers["x-spendgate-cost"] == "0.000050000 USD"
        swedish_hold, other_hold = (e.hold.nano for e in s.observer.held)
        assert swedish_hold == 3 * other_hold  # every Swedish meter is 3x


@async_test
async def test_a_streamed_call_records_the_regional_price_id_too(tmp_path: Path) -> None:
    async with open_stack(tmp_path, models=ROUTES, catalog_override=REGIONAL) as s:
        r = await s.chat("sweden", stream=True)
        assert r.status_code == 200 and ": spendgate" in r.text
        await s.pipeline.aclose()
        calls, _ = recorded(tmp_path, s.observer.held[0].request_id)
        assert calls == [f"{ID}@2026-11-01~swedencentral"]
