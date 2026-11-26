"""One route, one price (ADR-0028, point 4): start-up refuses a route whose deployments' Regions
resolve to different catalog entries, so the hold and the charge never depend on which one serves
(M-01: one hold per request at the price of every candidate)."""

from pathlib import Path

import pytest

from spendgate.cli import serve
from spendgate.cli.client import EXIT_CONFIG, EXIT_OK
from spendgate.config import ConfigError, check_config, load
from tests.unit.test_config import ENV

NOW_MS = 1_795_000_000_000  # 2026-11-18, after every 2026-11-01 entry below
CONFIG = """\
server: {listen: 127.0.0.1:8080, key_pepper: "env:SPENDGATE_KEY_PEPPER"}
ledger: {currency: USD, backend: {kind: sqlite, path: ./l.db}}
providers:
  azure_openai:
    base_url: "https://r.example.openai.azure.com"
    credential: "env:AZURE_OPENAI_API_KEY"
    region: eastus
catalog: {paths: [regional.yaml]}
models:
  m:
    catalog_id: azure_openai/gpt-x-test
    deployments:
"""


def entry(day: str = "2026-11-01", regions: str = "", price: str = "1") -> str:
    scope = f", regions: [{regions}]" if regions else ""
    return (
        f'  - {{id: azure_openai/gpt-x-test, mode: chat, currency: USD, effective_from: "{day}"'
        f'{scope}, source_url: "https://example.com/p", verified: true,'
        f' limits: {{max_output_tokens: 10}}, meters: {{input_per_mtok: "{price}",'
        f' output_per_mtok: "{price}"}}}}\n'
    )


def catalog(*entries: str) -> str:
    return "catalog_version: 1\nmodels:\n" + "".join(entries)


# A region-less price for every Region but two, which share a dearer one.
CATALOG = catalog(entry(), entry(regions="swedencentral, westeurope", price="1.1"))


def write(tmp_path: Path, *regions: str | None, prices: str = CATALOG) -> Path:
    lines = [
        "      - {provider: azure_openai" + (f", region: {r}" if r else "") + "}\n" for r in regions
    ]
    (tmp_path / "regional.yaml").write_text(prices, encoding="utf-8")
    path = tmp_path / "spendgate.yaml"
    path.write_text(CONFIG + "".join(lines), encoding="utf-8")
    return path


def test_m01_a_route_whose_regions_resolve_to_different_prices_is_refused(
    tmp_path: Path,
) -> None:
    path = write(tmp_path, "swedencentral", "francecentral")
    with pytest.raises(ConfigError) as info:
        load(path, env=ENV, now_ms=NOW_MS)
    message = str(info.value)
    assert "models.m.catalog_id" in message and "different prices" in message
    assert "region swedencentral -> azure_openai/gpt-x-test@2026-11-01~swedencentral" in message
    assert "region francecentral -> azure_openai/gpt-x-test@2026-11-01" in message
    assert "separate models" in message


def test_the_provider_region_counts_for_a_deployment_that_sets_none(tmp_path: Path) -> None:
    # providers.azure_openai.region is eastus: the second deployment is priced as eastus.
    report = check_config(write(tmp_path, "westeurope", None), env=ENV, now_ms=NOW_MS)
    assert not report.ok
    assert any("region westeurope" in e and "region eastus" in e for e in report.errors)


def test_check_config_and_serve_exit_3_naming_the_route_and_the_regions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    path = str(write(tmp_path, "westeurope", "eastus"))
    assert serve.run_check_config(path) == EXIT_CONFIG == 3
    assert serve.load_settings(path) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert err.count("models.m.catalog_id") == 2
    assert "region westeurope" in err and "region eastus" in err


def test_m01_regions_with_the_same_price_share_a_route(tmp_path: Path) -> None:
    path = write(tmp_path, "swedencentral", "westeurope")
    settings = load(path, env=ENV, now_ms=NOW_MS)
    assert [d.region for d in settings.routes["m"].deployments] == ["swedencentral", "westeurope"]
    assert check_config(path, env=ENV, now_ms=NOW_MS).ok
    assert load(write(tmp_path, "eastus", "francecentral"), env=ENV, now_ms=NOW_MS)


def test_a_valid_config_still_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    assert serve.run_check_config(str(write(tmp_path, "swedencentral", "westeurope"))) == EXIT_OK
    assert "configuration ok" in capsys.readouterr().out


def test_a_route_check_without_a_clock_still_finds_the_split(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="different prices"):
        load(write(tmp_path, "swedencentral", "eastus"), env=ENV)


def test_a_future_region_scoped_price_that_would_split_the_route_is_refused_now(
    tmp_path: Path,
) -> None:
    later = catalog(entry(), entry("2027-01-01", regions="swedencentral", price="1.1"))
    with pytest.raises(ConfigError, match="2027-01-01~swedencentral"):
        load(write(tmp_path, "swedencentral", "eastus", prices=later), env=ENV, now_ms=NOW_MS)


def test_a_region_no_entry_prices_is_unpriced_and_named(tmp_path: Path) -> None:
    scoped_only = catalog(entry(regions="swedencentral, westeurope"))
    path = write(tmp_path, "swedencentral", "eastus", prices=scoped_only)
    report = check_config(path, env=ENV, now_ms=NOW_MS)
    assert report.unpriced == ("azure_openai/gpt-x-test",)
    assert any("no price in the catalog for region eastus" in e for e in report.errors)
    with pytest.raises(ConfigError, match="for region eastus"):
        load(path, env=ENV)  # date-independent: no entry could ever price eastus
    only_eastus = check_config(write(tmp_path, "eastus", prices=scoped_only), env=ENV)
    assert any(e.endswith("has no price in the catalog") for e in only_eastus.errors)
