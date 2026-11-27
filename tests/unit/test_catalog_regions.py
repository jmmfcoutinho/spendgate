"""Region-scoped price entries (ADR-0028, point 3): resolution, load errors and the price id.

A Region-scoped entry prices only the Regions it lists (M-10: catalog data cannot fail open by
pricing a place it does not name); every other deployment falls back to the region-less entries.
"""

from datetime import UTC, datetime

import pytest
import yaml

from spendgate.core.catalog import Catalog, CatalogError, ModelNotPriced, parse
from spendgate.core.errors import ErrorCode
from tests.unit.test_catalog import bundled_text

SHA = "cd" * 32
ID = "azure_openai/gpt-x"


def ms(day: str) -> int:
    return int(datetime.fromisoformat(f"{day}T12:00:00+00:00").astimezone(UTC).timestamp()) * 1000


def entry(
    day: str = "2026-11-01", regions: list[str] | None = None, **over: object
) -> dict[str, object]:
    base: dict[str, object] = {
        "id": ID,
        "mode": "chat",
        "currency": "USD",
        "effective_from": day,
        "source_url": "https://example.com/pricing",
        "verified": True,
        "meters": {"input_per_mtok": "1", "output_per_mtok": "4"},
        "limits": {"max_output_tokens": 1000},
    }
    if regions is not None:
        base["regions"] = regions
    return base | over


def build(*documents: list[dict[str, object]]) -> Catalog:
    return parse(
        [{"catalog_version": 1, "models": m} for m in documents], SHA, ledger_currency="USD"
    )


def lookup(catalog: Catalog, region: str | None, day: str = "2026-11-20") -> str:
    found = catalog.lookup("azure_openai", "gpt-x", ms(day), region)
    return found.price_id_in(region)


def test_m10_a_listed_region_is_priced_by_its_region_scoped_entry() -> None:
    catalog = build([entry(), entry(regions=["swedencentral", "westeurope"], note="dearer")])
    assert lookup(catalog, "swedencentral") == f"{ID}@2026-11-01~swedencentral"
    assert lookup(catalog, "westeurope") == f"{ID}@2026-11-01~westeurope"


def test_m10_an_unlisted_region_falls_back_to_the_region_less_entry() -> None:
    catalog = build([entry(), entry(regions=["swedencentral"])])
    assert lookup(catalog, "eastus") == f"{ID}@2026-11-01"


def test_m10_a_region_scoped_entry_never_prices_a_region_it_does_not_list() -> None:
    catalog = build([entry(regions=["swedencentral"])])
    with pytest.raises(ModelNotPriced) as info:
        lookup(catalog, "eastus")
    assert info.value.refusal().code is ErrorCode.MODEL_NOT_PRICED


def test_m10_a_deployment_without_a_region_ignores_region_scoped_entries() -> None:
    with pytest.raises(ModelNotPriced):
        lookup(build([entry(regions=["swedencentral"])]), None)
    catalog = build([entry("2026-10-01"), entry("2026-11-01", regions=["swedencentral"])])
    assert lookup(catalog, None) == f"{ID}@2026-10-01"


def test_the_region_tier_is_chosen_before_the_date() -> None:
    # A newer region-less price does not displace an older Region-scoped one for that Region.
    catalog = build([entry("2026-10-01", regions=["swedencentral"]), entry("2026-11-01")])
    assert lookup(catalog, "swedencentral") == f"{ID}@2026-10-01~swedencentral"
    assert lookup(catalog, "eastus") == f"{ID}@2026-11-01"


def test_the_newest_effective_from_wins_within_the_tier() -> None:
    catalog = build(
        [
            entry("2026-09-01", regions=["swedencentral"]),
            entry("2026-11-01", regions=["swedencentral"]),
            entry("2026-12-01", regions=["swedencentral"]),  # not yet in force on 2026-11-20
            entry("2026-08-01"),
        ]
    )
    assert lookup(catalog, "swedencentral") == f"{ID}@2026-11-01~swedencentral"
    assert lookup(catalog, "swedencentral", "2026-12-02") == f"{ID}@2026-12-01~swedencentral"


def test_a_region_scoped_entry_not_yet_in_force_leaves_the_region_less_price() -> None:
    catalog = build([entry("2026-10-01"), entry("2026-12-01", regions=["swedencentral"])])
    assert lookup(catalog, "swedencentral") == f"{ID}@2026-10-01"


def test_a_deprecated_region_scoped_winner_is_refused_without_falling_back() -> None:
    expired = entry(regions=["swedencentral"], deprecation_date="2026-11-10")
    catalog = build([entry("2026-10-01"), expired])
    with pytest.raises(ModelNotPriced, match="deprecation"):
        lookup(catalog, "swedencentral")
    assert lookup(catalog, "eastus") == f"{ID}@2026-10-01"


def test_overlapping_regions_on_one_date_are_a_load_error() -> None:
    with pytest.raises(CatalogError, match="region westeurope in two entries"):
        build([entry(regions=["swedencentral", "westeurope"]), entry(regions=["westeurope"])])


def test_overlapping_regions_across_documents_are_a_load_error() -> None:
    with pytest.raises(CatalogError, match=r"overlap.*region westeurope"):
        build([entry(regions=["westeurope"])], [entry(regions=["westeurope", "eastus"])])


def test_two_region_less_entries_on_one_date_are_a_load_error() -> None:
    with pytest.raises(CatalogError, match="duplicate"):
        build([entry(), entry(note="again")])


def test_the_same_region_set_twice_in_one_file_is_a_load_error() -> None:
    with pytest.raises(CatalogError, match=r"duplicate .*~eastus,westeurope"):
        build([entry(regions=["westeurope", "eastus"]), entry(regions=["eastus", "westeurope"])])


def test_disjoint_regions_on_one_date_and_one_region_less_entry_coexist() -> None:
    catalog = build([entry(), entry(regions=["swedencentral"]), entry(regions=["westeurope"])])
    assert len(catalog.entries()) == 3


def test_an_override_replaces_the_entry_with_the_same_regions_and_keeps_the_others() -> None:
    bundled = [entry(), entry(regions=["swedencentral"])]
    override = [
        entry(regions=["swedencentral"], meters={"input_per_mtok": "9", "output_per_mtok": "9"})
    ]
    catalog = build(bundled, override)
    swedish = catalog.lookup("azure_openai", "gpt-x", ms("2026-11-20"), "swedencentral")
    assert swedish.meters.input_per_mtok == 9
    assert len(catalog.entries()) == 2


def test_regions_must_be_a_non_empty_list_of_distinct_slugs() -> None:
    for bad in ([], ["eastus", "eastus"], ["EastUS"], "eastus"):
        with pytest.raises(CatalogError, match="regions"):
            build([entry(regions=bad)])  # type: ignore[arg-type]


def test_the_price_id_carries_the_region_only_for_a_region_scoped_entry() -> None:
    catalog = build([entry(), entry(regions=["swedencentral"])])
    plain = catalog.lookup("azure_openai", "gpt-x", ms("2026-11-20"), "eastus")
    scoped = catalog.lookup("azure_openai", "gpt-x", ms("2026-11-20"), "swedencentral")
    assert plain.price_id_in("eastus") == plain.price_id_in(None) == f"{ID}@2026-11-01"
    assert scoped.price_id_in("swedencentral") == f"{ID}@2026-11-01~swedencentral"
    assert scoped.price_id == f"{ID}@2026-11-01"  # the (id, effective_from) pair is unchanged


def test_m01_a_route_resolves_to_one_entry_or_is_refused() -> None:
    catalog = build([entry(), entry(regions=["swedencentral", "westeurope"])])
    now = ms("2026-11-20")
    same = catalog.lookup_route("azure_openai", "gpt-x", ["swedencentral", "westeurope"], now)
    assert same.regions == ("swedencentral", "westeurope")
    assert catalog.lookup_route("azure_openai", "gpt-x", [], now).regions is None
    with pytest.raises(ModelNotPriced, match="different prices"):
        catalog.lookup_route("azure_openai", "gpt-x", ["swedencentral", "eastus"], now)


def test_a_route_split_is_found_on_the_date_a_region_scoped_entry_starts() -> None:
    catalog = build([entry("2026-10-01"), entry("2026-12-01", regions=["swedencentral"])])
    regions = ["swedencentral", "eastus"]
    split = catalog.split_by_region("azure_openai", "gpt-x", regions, ms("2026-11-20"))
    assert split is not None
    assert {r: e.price_id_in(r) for r, e in split.items()} == {
        "swedencentral": f"{ID}@2026-12-01~swedencentral",
        "eastus": f"{ID}@2026-10-01",
    }
    assert catalog.split_by_region("azure_openai", "gpt-x", regions, None) is not None
    assert catalog.split_by_region("azure_openai", "gpt-x", ["eastus", None], None) is None


def test_the_bundled_catalog_scopes_entries_to_regions_only_where_an_importer_wrote_them() -> None:
    catalog = parse([yaml.safe_load(bundled_text())], SHA, ledger_currency="USD")
    scoped = {e.id for e in catalog.entries() if e.regions is not None}
    assert scoped  # Bedrock and Azure price many profiles and deployments per Region
    assert {i.partition("/")[0] for i in scoped} <= {"bedrock", "azure_openai", "google"}
    azure = {i for i in scoped if i.startswith("azure_openai/")}
    generated = ("azure_openai/global.", "azure_openai/datazone.", "azure_openai/regional.")
    assert all(i.startswith(generated) for i in azure)  # the hand-written bare ids stay region-less
