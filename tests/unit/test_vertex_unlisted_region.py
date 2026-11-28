"""A Vertex AI deployment is never priced by a region-less entry (ADR-0034, amending ADR-0028).

For a `google` id the region-less entries are the Gemini API's prices, a different product; Vertex
bills its own (Region-scoped) prices. So a Vertex deployment (`auth: bearer_token`) in a Region no
entry lists is refused at start, and at call time, instead of being held and booked at the Gemini
API price (M-10: catalog data cannot fail open). Other deployments keep the ADR-0028 fallback,
with the start-up warning.
"""

from pathlib import Path

import pytest

from spendgate.cli import serve
from spendgate.cli.client import EXIT_CONFIG
from spendgate.config import ConfigError, check_config, load
from spendgate.core.catalog import ModelNotPriced
from spendgate.core.errors import ErrorCode
from tests.unit.test_config import ENV

NOW_MS = 1_795_000_000_000  # 2026-11-18, after every 2026-11-01 entry below
LATER_MS = 1_830_000_000_000  # 2027-12-28, after the 2027-01-01 entry below
GOOGLE_ENV = ENV | {"GOOGLE_API_KEY": "fake-gemini-key-0123"}  # pragma: allowlist secret
CONFIG = """\
server: {listen: 127.0.0.1:8080, key_pepper: "env:SPENDGATE_KEY_PEPPER"}
ledger: {currency: USD, backend: {kind: sqlite, path: ./l.db}}
providers:
  google: {base_url: "https://vertex.example.test", PROVIDER}
catalog: {paths: [prices.yaml]}
models:
  g: {catalog_id: google/gemini-x-test, deployments: [{provider: google}]}
"""
VERTEX = "auth: bearer_token, credential: adc"
GEMINI_API = 'credential: "env:GOOGLE_API_KEY"'


def entry(regions: str = "", price: str = "4", day: str = "2026-11-01") -> str:
    scope = f", regions: [{regions}]" if regions else ""
    return (
        f'  - {{id: google/gemini-x-test, mode: chat, currency: USD, effective_from: "{day}"'
        f'{scope}, source_url: "https://example.com/p", verified: true,'
        f' limits: {{max_output_tokens: 10}}, meters: {{input_per_mtok: "{price}",'
        f' output_per_mtok: "{price}"}}}}\n'
    )


# The bundled shape: a region-less Gemini API price, a Vertex global and a dearer non-global one.
PRICES = entry() + entry("global") + entry("eu, us", price="4.4")


def write(tmp_path: Path, provider: str, region: str | None, prices: str = PRICES) -> Path:
    where = f", region: {region}" if region else ""
    (tmp_path / "prices.yaml").write_text(
        "catalog_version: 1\nmodels:\n" + prices, encoding="utf-8"
    )
    path = tmp_path / "spendgate.yaml"
    path.write_text(CONFIG.replace("PROVIDER", provider + where), encoding="utf-8")
    return path


def test_m10_a_vertex_deployment_in_an_unlisted_region_is_not_priced_at_the_gemini_api_price(
    tmp_path: Path,
) -> None:
    path = write(tmp_path, VERTEX, "us-central1")
    with pytest.raises(ConfigError) as info:
        load(path, env=GOOGLE_ENV, now_ms=NOW_MS)
    message = str(info.value)
    assert "models.g.catalog_id" in message
    assert "google/gemini-x-test has no Vertex AI price for region us-central1" in message
    assert "never by the region-less Gemini API price" in message
    assert "Regions listed for that id: eu, global, us" in message
    report = check_config(path, env=GOOGLE_ENV, now_ms=NOW_MS)
    assert report.unpriced == ("google/gemini-x-test",) and report.unlisted_regions == ()
    with pytest.raises(ConfigError, match="no Vertex AI price for region us-central1"):
        load(path, env=GOOGLE_ENV)  # date-independent: no entry could ever price it


def test_check_config_and_serve_exit_3_for_a_vertex_route_in_an_unlisted_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in GOOGLE_ENV.items():
        monkeypatch.setenv(name, value)
    path = str(write(tmp_path, VERTEX, "europe-west4"))
    assert serve.run_check_config(path) == EXIT_CONFIG == 3
    assert serve.load_settings(path) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert err.count("models.g.catalog_id") == 2 and "region europe-west4" in err


def test_a_vertex_deployment_in_a_listed_region_keeps_its_region_scoped_price(
    tmp_path: Path,
) -> None:
    settings = load(write(tmp_path, VERTEX, "us"), env=GOOGLE_ENV, now_ms=NOW_MS)
    route = settings.routes["g"]
    assert route.listed_only
    assert route.price(settings.catalog, NOW_MS).price_id_in("us") == (
        "google/gemini-x-test@2026-11-01~us"
    )


def test_m10_a_vertex_deployment_of_an_id_with_only_a_region_less_price_is_refused(
    tmp_path: Path,
) -> None:
    path = write(tmp_path, VERTEX, "us", prices=entry())
    with pytest.raises(ConfigError, match="no entry of it lists one"):
        load(path, env=GOOGLE_ENV, now_ms=NOW_MS)


def test_m10_a_vertex_region_listed_only_from_a_later_date_is_not_priced_before_it(
    tmp_path: Path,
) -> None:
    later = entry() + entry("us", price="4.4", day="2027-01-01")
    path = write(tmp_path, VERTEX, "us", prices=later)
    with pytest.raises(ConfigError, match="no Vertex AI price for region us"):
        load(path, env=GOOGLE_ENV, now_ms=NOW_MS)
    settings = load(path, env=GOOGLE_ENV)  # date-independent: an entry will price us
    route = settings.routes["g"]
    with pytest.raises(ModelNotPriced) as info:  # at call time: refused, not the Gemini API price
        route.price(settings.catalog, NOW_MS)
    assert info.value.refusal().code is ErrorCode.MODEL_NOT_PRICED
    assert "lists the deployment's region" in info.value.reason
    assert route.price(settings.catalog, LATER_MS).price_id_in("us").endswith("~us")


def test_a_gemini_api_deployment_still_takes_the_region_less_price(tmp_path: Path) -> None:
    settings = load(write(tmp_path, GEMINI_API, None), env=GOOGLE_ENV, now_ms=NOW_MS)
    route = settings.routes["g"]
    assert not route.listed_only
    assert route.price(settings.catalog, NOW_MS).price_id_in(None) == (
        "google/gemini-x-test@2026-11-01"
    )


def test_a_non_vertex_deployment_in_an_unlisted_region_still_falls_back_with_a_warning(
    tmp_path: Path,
) -> None:
    # The ADR-0028 fallback stays for every other deployment (Azure: test_config_regions.py).
    path = write(tmp_path, GEMINI_API, "us-central1")
    settings = load(path, env=GOOGLE_ENV, now_ms=NOW_MS)
    entry_ = settings.routes["g"].price(settings.catalog, NOW_MS)
    assert entry_.regions is None and entry_.price_id_in("us-central1").endswith("@2026-11-01")
    (line,) = settings.unlisted_regions()
    assert "region 'us-central1' is listed by no entry" in line and "region-less price" in line
