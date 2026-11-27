"""`spendgate catalog draft --regions/--region`: Region-scoped stand-ins (ADR-0028 points 2, 3).

No network: the bundled catalog and override files under tmp_path, the clock passed in.
"""

from pathlib import Path

import pytest

from spendgate.cli import main
from spendgate.cli.client import EXIT_CONFIG, EXIT_OK, EXIT_USAGE
from spendgate.core import standin
from spendgate.core.catalog import ModelNotPriced
from tests.unit.test_catalog_draft import (
    LIKE,
    NEW,
    NOON,
    bundled,
    loaded,
    models,
    override,
    own,
    run,
)


def scoped(entry: str, regions: str) -> str:
    return f"{entry}    regions: [{regions}]\n"


PLAIN = '{input_per_mtok: "1", output_per_mtok: "4"}'
DEARER = '{input_per_mtok: "3", output_per_mtok: "12"}'


def test_a_region_scoped_stand_in_drafts_loads_and_prices_only_its_regions(tmp_path: Path) -> None:
    out = tmp_path / "stand-in.yaml"
    regions = "westeurope, swedencentral,westeurope"
    assert run("--like", LIKE, "--as", NEW, "--regions", regions, "--out", str(out)) == EXIT_OK
    [entry] = models(out)
    assert entry["regions"] == ["swedencentral", "westeurope"]  # sorted, deduplicated
    assert entry["note"] == (
        f"Stand-in: priced like {bundled(LIKE).price_id} x 1 for Regions swedencentral, "
        "westeurope; replace with the provider's price"
    )
    catalog = loaded(out)
    found = catalog.lookup("anthropic", "claude-q4-test", NOON, "swedencentral")
    assert found.regions == ("swedencentral", "westeurope") and found.verified is False
    assert found.price_id_in("swedencentral") == "anthropic/claude-q4-test@2026-11-26~swedencentral"
    for elsewhere in ("eastus", None):  # a scoped price is never used for a Region it omits
        with pytest.raises(ModelNotPriced):
            catalog.lookup("anthropic", "claude-q4-test", NOON, elsewhere)


def test_a_region_scoped_stand_in_beside_a_region_less_price(tmp_path: Path) -> None:
    config = override(tmp_path, own("openai/own", "2026-11-01", PLAIN))
    out = tmp_path / "stand-in.yaml"
    argv = ("--like", "openai/own", "--as", "openai/own", "--multiplier", "1.5")
    assert run(*argv, "--regions", "eastus", "--config", str(config), "--out", str(out)) == 0
    catalog = loaded(tmp_path / "own.yaml", out)
    assert catalog.lookup("openai", "own", NOON, "eastus").meters.input_per_mtok == 1.5
    assert catalog.lookup("openai", "own", NOON, "westus").meters.input_per_mtok == 1


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--regions", ""),
        ("--regions", ","),
        ("--regions", "swedencentral,"),
        ("--regions", "West-Europe"),
        ("--regions", "eu_west_1"),
        ("--regions", "sweden central"),
        ("--regions", "eastus;rm"),
        ("--region", "EastUS"),
        ("--region", ""),
    ],
)
def test_a_bad_region_slug_is_refused(
    tmp_path: Path, flag: str, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "stand-in.yaml"
    argv = ["catalog", "draft", "--like", LIKE, "--as", NEW, f"{flag}={value}", "--out", str(out)]
    assert main(argv) == EXIT_USAGE
    assert not out.exists()
    assert capsys.readouterr().err.startswith(f"error: {flag}: ")


def test_the_slug_pattern_is_the_schemas() -> None:
    assert standin.parse_regions("eu-west-1,swedencentral") == ("eu-west-1", "swedencentral")
    with pytest.raises(standin.StandInError):
        standin.parse_regions("eu.west")


def test_a_region_another_entry_of_the_id_and_date_lists_is_refused(tmp_path: Path) -> None:
    taken = scoped(own("openai/own", "2026-11-26", PLAIN), "swedencentral, eastus")
    config = override(tmp_path, own("openai/base", "2026-11-01", PLAIN), taken)
    out = tmp_path / "stand-in.yaml"
    argv = ("--like", "openai/base", "--as", "openai/own", "--config", str(config))
    assert run(*argv, "--regions", "swedencentral", "--out", str(out)) == EXIT_CONFIG
    assert not out.exists()
    # The same set of Regions is the same entry: the stand-in replaces it, as any override does.
    assert run(*argv, "--regions", "eastus,swedencentral", "--out", str(out)) == EXIT_OK


def test_a_region_scoped_like_needs_region_to_pick_its_price(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = override(tmp_path, scoped(own("openai/own", "2026-11-01", DEARER), "westeurope"))
    out = tmp_path / "stand-in.yaml"
    argv = ["--like", "openai/own", "--as", "openai/new", "--config", str(config)]
    assert main(["catalog", "draft", *argv, "--out", str(out)]) == EXIT_USAGE
    assert "priced for --region westeurope" in capsys.readouterr().err
    assert run(*argv, "--region", "eastus", "--out", str(out)) == EXIT_USAGE
    assert run(*argv, "--region", "westeurope", "--out", str(out)) == EXIT_OK
    [entry] = models(out)
    assert entry["meters"] == {"input_per_mtok": "3", "output_per_mtok": "12"}
    assert "regions" not in entry  # --region picks the source; only --regions scopes the draft
    assert "priced like openai/own@2026-11-01~westeurope x 1;" in entry["note"]


def test_region_picks_the_price_and_follows_its_schedule_in_that_region(tmp_path: Path) -> None:
    config = override(
        tmp_path,
        own("openai/own", "2026-11-01", PLAIN),
        scoped(own("openai/own", "2026-12-01", DEARER), "westeurope"),
    )
    argv = ("--like", "openai/own", "--as", "openai/new", "--config", str(config))
    there, plain = tmp_path / "there.yaml", tmp_path / "plain.yaml"
    assert run(*argv, "--region", "westeurope", "--out", str(there)) == EXIT_OK
    assert [(e["effective_from"], e["meters"]["input_per_mtok"]) for e in models(there)] == [
        ("2026-11-26", "1"),
        ("2026-12-01", "3"),
    ]
    assert run(*argv, "--out", str(plain)) == EXIT_OK
    assert [e["meters"]["input_per_mtok"] for e in models(plain)] == ["1"]  # not in westeurope


def test_an_id_already_priced_in_a_listed_region_is_warned_about(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = override(tmp_path, scoped(own("openai/own", "2026-11-01", PLAIN), "eastus"))
    out = tmp_path / "stand-in.yaml"
    argv = ("--like", "openai/gpt-6-luna", "--as", "openai/own", "--config", str(config))
    assert run(*argv, "--regions", "eastus,westus", "--out", str(out)) == EXIT_OK
    err = capsys.readouterr().err
    assert "already priced in region eastus (openai/own@2026-11-01~eastus)" in err
    assert "westus" not in err


def test_a_later_date_past_the_copied_models_deprecation_ends_the_schedule(
    tmp_path: Path,
) -> None:
    retiring = own("openai/own", "2026-11-01", PLAIN) + '    deprecation_date: "2026-12-01"\n'
    config = override(tmp_path, retiring, scoped(own("openai/own", "2026-12-10", DEARER), "eu"))
    out = tmp_path / "stand-in.yaml"
    argv = ("--like", "openai/own", "--as", "openai/new", "--config", str(config))
    assert run(*argv, "--out", str(out)) == EXIT_OK
    [entry] = models(out)  # the stand-in keeps its price; it has no deprecation date of its own
    assert "deprecation_date" not in entry
    assert entry["meters"]["input_per_mtok"] == "1"
