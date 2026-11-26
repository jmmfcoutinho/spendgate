"""The example catalog entry in docs/catalog-how-to.md is valid and prices a call."""

import json
import re
from decimal import Decimal
from importlib import resources
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from spendgate.core.catalog import parse
from spendgate.core.cost import Usage, actual_cost
from spendgate.core.money import Money

DOC = Path(__file__).resolve().parents[2] / "docs" / "catalog-how-to.md"
BLOCK = re.compile(r"<!-- doctest: catalog -->\n```yaml\n(.*?)```", re.DOTALL)
REGIONS_BLOCK = re.compile(r"<!-- doctest: catalog-regions -->\n```yaml\n(.*?)```", re.DOTALL)
SCHEMA = json.loads((resources.files("spendgate.catalog") / "schema.json").read_text("utf-8"))
JAN_2027_MS = 1_798_761_600_000  # after the entry's effective_from


def test_the_catalog_example_passes_the_schema_and_prices_a_call() -> None:
    match = BLOCK.search(DOC.read_text())
    assert match, "docs/catalog-how-to.md lost its `doctest: catalog` block"
    document = yaml.safe_load(match.group(1))
    Draft202012Validator(SCHEMA).validate(document)
    catalog = parse([document], "0" * 64, ledger_currency="USD")
    entry = catalog.lookup("openai", "example-model", JAN_2027_MS)
    # (1000 * 1.50 + 500 * 6) / 1e6 = 0.0045 USD
    assert actual_cost(entry, Usage(input_tokens=1000, output_tokens=500)) == Money(
        4_500_000, "USD"
    )


def test_the_region_scoped_example_prices_its_regions_and_leaves_the_others() -> None:
    match = REGIONS_BLOCK.search(DOC.read_text())
    assert match, "docs/catalog-how-to.md lost its `doctest: catalog-regions` block"
    document = yaml.safe_load(match.group(1))
    Draft202012Validator(SCHEMA).validate(document)
    catalog = parse([document], "0" * 64, ledger_currency="USD")
    sweden = catalog.lookup("azure_openai", "example-model", JAN_2027_MS, "swedencentral")
    elsewhere = catalog.lookup("azure_openai", "example-model", JAN_2027_MS, "eastus")
    assert (
        sweden.price_id_in("swedencentral") == "azure_openai/example-model@2026-11-01~swedencentral"
    )
    assert elsewhere.price_id_in("eastus") == "azure_openai/example-model@2026-11-01"
    assert sweden.meters.input_per_mtok == Decimal("2.2")
