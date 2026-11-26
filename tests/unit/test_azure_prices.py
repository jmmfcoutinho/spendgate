"""scripts/azure_prices.py: meter names, units, deployment types, Regions, output, --check (#106).

No network. `tests/data/azure_retail/items.json` holds 64 real items of the Azure Retail Prices API
(`serviceName eq 'Foundry Models'`, downloaded 2026-11-26), one partner model among them.
Edge cases mutate copies of those items.
"""
# ruff: noqa: ANN401  (the script is loaded dynamically, so its types are Any here)

import copy
import importlib.util
import io
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from importlib import resources
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from spendgate.core.catalog import parse

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "tests" / "data" / "azure_retail" / "items.json"
CHECKED = date(2026, 11, 26)
NOW_MS = 1_795_651_200_000  # 2026-11-26


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("azure_prices", REPO / "scripts/azure_prices.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["azure_prices"] = module
    spec.loader.exec_module(module)
    return module


ap = load_script()


def items() -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = ap.load_items(DATA.read_bytes())
    return copy.deepcopy(loaded)


def entries(data: list[dict[str, Any]] | None = None) -> dict[str, list[Any]]:
    found: dict[str, list[Any]] = {}
    for entry in ap.build(ap.read_items(items() if data is None else data)).entries:
        found.setdefault(ap.catalog_id(entry), []).append(entry)
    return found


def meters(entry: Any) -> dict[str, str]:
    return dict(entry.body[0])


def item(data: list[dict[str, Any]], name: str, region: str) -> dict[str, Any]:
    return next(i for i in data if i["meterName"] == name and i["armRegionName"] == region)


# --- meter names -------------------------------------------------------------------------------

IN, CACHED, WRITE, OUT = (
    "input_per_mtok",
    "cached_input_per_mtok",
    "cache_write_per_mtok",
    "output_per_mtok",
)
AUDIO, MINUTE, CHARS = "audio_input_per_mtok", "audio_input_per_minute", "per_1k_characters"
GL, DZ, RG = "global", "data_zone", "regional"
CLASSIFIED = [
    # GPT-6 style: model, context tier, meter, "Std", deployment type, unit in the name
    ("6-luna ShortCo Inp Std Gl 1M Tokens", "gpt-6-luna", GL, IN, ""),
    ("6-luna LongCo Cd Inp Std DZ 1M Tokens", "gpt-6-luna", DZ, CACHED, "long"),
    ("6-luna ShortCo Cd Wr Std DZ 1M Tokens", "gpt-6-luna", DZ, WRITE, ""),
    ("61 terra LongCo Opt std Gl 1M Tokens", "gpt-6.1-terra", GL, OUT, "long"),
    ("6.2-astra LoCo Cd Wr Std Gl 1M Tokens", "gpt-6.2-astra", GL, WRITE, "long"),
    # GPT-5 styles
    ("5.4 nano Inp Dz 1M Tokens", "gpt-5.4-nano", DZ, IN, ""),
    ("5.4 longco cd inp Gl 1M Tokens", "gpt-5.4", GL, CACHED, "long"),
    ("GPT 5 Mini cchd Inpt DZone 1M Tokens", "gpt-5-mini", DZ, CACHED, ""),
    ("GPT 5 outpt Glbl 1M Tokens", "gpt-5", GL, OUT, ""),
    ("gpt-5-codex-ccchd-inp-dzone Tokens", "gpt-5-codex", DZ, CACHED, ""),
    ("gpt 5 pro out dzone Tokens", "gpt-5-pro", DZ, OUT, ""),
    ("chat-latest 10072026 cd inp Dz 1M Tokens", "gpt-chat-latest-2026-10-07", DZ, CACHED, ""),
    # GPT-4 styles: words with spaces or hyphens, "Data Zone" in two words, 1K units
    ("gpt 4.1 nano cached Inp glbl Tokens", "gpt-4.1-nano", GL, CACHED, ""),
    ("gpt 4.1 Outp Data Zone Tokens", "gpt-4.1", DZ, OUT, ""),
    ("gpt-4o-mini-0718-Inp-regnl Tokens", "gpt-4o-mini", RG, IN, ""),
    ("gpt 4o mini 0718 cached Inp DZone Tokens", "gpt-4o-mini", DZ, CACHED, ""),
    ("gpt 4o 0806 Inp Data Zone Tokens", "gpt-4o-2024-08-06", DZ, IN, ""),
    ("gpt-4o-0806-Outp-glbl Tokens", "gpt-4o-2024-08-06", GL, OUT, ""),
    ("gpt-oss-120B Outp DZone Tokens", "gpt-oss-120b", DZ, OUT, ""),
    # embeddings, transcription and speech
    ("text-embedding-3-large-regional Tokens", "text-embedding-3-large", RG, IN, ""),
    ("text embedding 3 small DZ Tokens", "text-embedding-3-small", DZ, IN, ""),
    ("embedding-ada-glbl-new Tokens", "text-embedding-ada-002", GL, IN, ""),
    ("gpt-transcribe Dz Unit", "gpt-transcribe", DZ, MINUTE, ""),
    ("gpt-4o-transcribe-aud-inp-rgnl Tokens", "gpt-4o-transcribe", RG, AUDIO, ""),
    ("gpt4o mn trscb txt out gl 1215 1M Tokens", "gpt-4o-mini-transcribe", GL, OUT, ""),
    ("gpt 4o tcrb d txt inp dz Tokens", "gpt-4o-transcribe-diarize", DZ, IN, ""),
    ("Speech-Text to Speech HD-Regional Characters", "tts-hd", RG, CHARS, ""),
]  # fmt: skip


@pytest.mark.parametrize(("name", "model", "dtype", "meter", "tier"), CLASSIFIED)
def test_each_meter_name_style_maps_to_model_deployment_type_and_meter(
    name: str, model: str, dtype: str, meter: str, tier: str
) -> None:
    assert ap.classify(name) == ap.Meter(model, dtype, meter, tier)


SKIPPED = [
    ("o4-mini 0416 Batch Outp Data Zone Tokens", "Batch"),
    ("gpt 4o mini0718 BatchOutp DataZone Tokens", "Batch"),
    ("56sol ShCo Cd Wr 12hr Dz 1M Tokens", "12-hour cache write"),
    ("56luna LoCo CdWr12h Gl 1M Tokens", "12-hour cache write"),
    ("6-luna ShortCo Inp Flex Gl 1M Tokens", "Flex"),
    ("6astra ShCo Cd Wr 12hr Fl Gl 1M Tokens", "Flex"),
    ("5.4 mini pp Inp Gl 1M Tokens", "Priority processing"),
    ("6-sol ShortCo Inp PP DZ 1M Tokens", "Priority processing"),
    ("gpt 4.1 dev ft training glbl Tokens", "fine-tuning"),
    ("gpt-4o-rt-aud-0603 Inp glbl Tokens", "Realtime"),
    ("gpt aud 1.5 inp Gl 1M Tokens", "audio chat model"),
    ("gpt-image-1-inp-txt-glbl Tokens", "image"),
    ("o3 0416 Inp glbl Tokens", "o3 0416: retired"),
    ("gpt 5.2 pro inp Gl 1M Tokens", "gpt 5.2 pro: not on Microsoft's models page"),
    ("gpt-4o-mini-tts-aud-out-glbl Tokens", "gpt 4o mini tts: billed per audio token"),
    ("gpt-live-transcribe Gl Unit", "gpt live transcribe: a Realtime API model"),
    ("Az-GPT-3.5-turbo Tokens", "legacy"),
    ("gpt 4o 0513 Output regional Tokens", "gpt-4o-2024-05-13 Regional Standard: retired"),
    ("56 sol ShortCo Cd Inp Std Rg 1M Tokens", "56 sol: left out like openai/gpt-5.6-sol"),
    ("5.6 sol LongCo Inp Std DZ 1M Tokens", "5.6 sol: left out like openai/gpt-5.6-sol"),
]


@pytest.mark.parametrize(("name", "reason"), SKIPPED)
def test_variants_and_models_not_imported_are_skipped_with_their_reviewed_reason(
    name: str, reason: str
) -> None:
    found = ap.classify(name)
    assert isinstance(found, ap.Skip)
    assert found.reason.startswith(reason)


def test_the_short_abbreviations_of_each_deployment_type_are_read() -> None:
    assert ap.words("56 sol ShortCo Cd Inp Std Rg 1M Tokens").dtype == "regional"
    assert ap.words("gpt 4o 0806 Inp Data Zone Tokens").dtype == "data_zone"
    assert ap.words("6-luna ShortCo Inp Std Gl 1M Tokens").dtype == "global"


@pytest.mark.parametrize(
    "name",
    [
        "gpt 9 turbo Inp glbl Tokens",  # a model nobody reviewed
        "6-luna ShortCo Inp Std 1M Tokens",  # no deployment type
        "6-luna ShortCo Std Gl 1M Tokens",  # no meter
        "6-luna ShortCo Inp Gl DZ 1M Tokens",  # two deployment types
        "gpt 4.1 cached Outp glbl Tokens",  # no such meter
        "gpt 4.1 LongCo Inp glbl Tokens",  # long context on a model without a threshold
        "gpt-4o-transcribe-inp-glbl Tokens",  # neither audio nor text
    ],
)
def test_a_meter_the_mapping_cannot_classify_stops_the_run(name: str) -> None:
    with pytest.raises(ap.ImportFailure):
        ap.classify(name)


def test_an_unknown_azure_openai_product_stops_the_run_and_partner_models_are_ignored() -> None:
    data = items()
    assert any(i["productName"] == "Azure Baseten Models" for i in data)  # ignored, no failure
    ap.read_items(data)
    data[0] = {**data[0], "productName": "Azure OpenAI GPT7"}
    with pytest.raises(ap.ImportFailure, match="unknown Azure OpenAI product"):
        ap.read_items(data)


def test_an_unknown_meter_in_the_price_list_stops_the_run() -> None:
    data = items()
    data.append({**data[1], "meterName": "gpt 9 turbo Inp glbl Tokens"})
    with pytest.raises(ap.ImportFailure, match="unknown Azure OpenAI meter 'gpt 9 turbo"):
        ap.read_items(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("currencyCode", "EUR"),
        ("armRegionName", "Global"),
        ("retailPrice", Decimal(0)),
        ("tierMinimumUnits", Decimal(1000)),
        ("isPrimaryMeterRegion", False),
        ("unitOfMeasure", "1 Hour"),
    ],
)
def test_an_item_that_is_not_one_plain_usd_price_per_region_stops_the_run(
    field: str, value: object
) -> None:
    data = items()
    target = item(data, "6-luna ShortCo Inp Std Gl 1M Tokens", "eastus2")
    target[field] = value
    with pytest.raises(ap.ImportFailure):
        ap.read_items(data)


# --- units -------------------------------------------------------------------------------------


def test_1k_and_1m_units_convert_exactly_to_per_million_decimals() -> None:
    nano = entries()["azure_openai/global.gpt-4.1-nano"]
    # 0.0001, 0.000025 and 0.0004 USD per 1K tokens, as Decimal: no binary float involved
    assert meters(nano[0]) == {
        "input_per_mtok": "0.1",
        "cached_input_per_mtok": "0.025",
        "output_per_mtok": "0.4",
    }
    assert ap.per_unit("input_per_mtok", "1K", Decimal("0.000028"), "x") == Decimal("0.028")
    assert ap.per_unit("input_per_mtok", "1M", Decimal("0.11"), "x") == Decimal("0.11")


def test_hours_become_minutes_and_characters_become_thousands() -> None:
    found = entries()
    assert meters(found["azure_openai/global.gpt-transcribe"][0]) == {
        "audio_input_per_minute": "0.0045"  # 0.27 USD per hour
    }
    assert meters(found["azure_openai/global.tts"][0]) == {"per_1k_characters": "0.015"}
    # a per-hour price that does not divide by 60 is rounded up, never down
    assert ap.per_unit("audio_input_per_minute", "1 Hour", Decimal("1"), "x") == Decimal(
        "0.016666666667"
    )


# --- deployment types, Regions and long context ------------------------------------------------


def test_each_deployment_type_gets_its_own_id() -> None:
    found = entries()
    assert {i for i in found if i.endswith(".gpt-6-luna")} == {
        "azure_openai/global.gpt-6-luna",
        "azure_openai/datazone.gpt-6-luna",
    }
    assert {e.dtype for e in found["azure_openai/regional.gpt-4.1-nano"]} == {"regional"}


def test_a_price_the_same_in_every_region_is_one_entry_without_regions() -> None:
    (world,) = entries()["azure_openai/global.gpt-6-luna"]
    assert world.regions == ()
    assert meters(world) == {
        "input_per_mtok": "0.1",
        "cached_input_per_mtok": "0.01",
        "cache_write_per_mtok": "0.125",
        "output_per_mtok": "0.5",
    }
    assert world.body[1] == ("2", "1.5")  # long context: 0.2 / 0.1 input, 0.75 / 0.5 output


def test_prices_that_differ_by_region_become_region_scoped_entries_and_no_regionless_one() -> None:
    zone = entries()["azure_openai/datazone.gpt-6-luna"]
    assert [(e.regions, meters(e)["input_per_mtok"]) for e in zone] == [
        (("eastus2",), "0.11"),
        (("swedencentral",), "0.12"),
    ]
    regional = entries()["azure_openai/regional.gpt-4.1-nano"]
    assert [(e.regions, meters(e)["output_per_mtok"]) for e in regional] == [
        (("eastus2",), "0.44"),
        (("swedencentral",), "0.484"),
    ]


def test_a_region_missing_a_meter_the_model_has_elsewhere_is_left_unpriced() -> None:
    data = [
        i
        for i in items()
        if (i["meterName"], i["armRegionName"])
        != ("6-luna LongCo Cd Wr Std DZ 1M Tokens", "swedencentral")
    ]
    built = ap.build(ap.read_items(data))
    zone = [e for e in built.entries if ap.catalog_id(e) == "azure_openai/datazone.gpt-6-luna"]
    assert [e.regions for e in zone] == [("eastus2",)]  # still scoped: Sweden is refused
    assert built.dropped == [
        "gpt-6-luna data_zone swedencentral: no cache_write_per_mtok (long) meter"
    ]


def test_a_missing_cached_input_meter_is_not_a_reason_to_leave_a_region_out() -> None:
    data = [i for i in items() if i["meterName"] != "gpt 4o mini 0718 cached Inp glbl Tokens"]
    (mini,) = entries(data)["azure_openai/global.gpt-4o-mini"]
    assert meters(mini) == {"input_per_mtok": "0.15", "output_per_mtok": "0.6"}


def test_two_meters_that_price_the_same_thing_must_agree() -> None:
    data = items()
    assert meters(entries(data)["azure_openai/global.text-embedding-ada-002"][0]) == {
        "input_per_mtok": "0.1"
    }
    item(data, "embedding-ada-glbl-new Tokens", "eastus2")["retailPrice"] = Decimal("0.00011")
    with pytest.raises(ap.ImportFailure, match="price one meter differently"):
        ap.read_items(data)


def test_long_context_multipliers_must_be_uniform_across_meters() -> None:
    data = items()
    item(data, "6-luna LongCo Cd Wr Std Gl 1M Tokens", "eastus2")["retailPrice"] = Decimal("0.3")
    with pytest.raises(ap.ImportFailure, match="multipliers differ"):
        ap.build(ap.read_items(data))


def test_effective_from_is_the_latest_start_date_of_the_entry_meters() -> None:
    data = items()
    item(data, "6-luna ShortCo Inp Std DZ 1M Tokens", "eastus2")["effectiveStartDate"] = (
        "2026-10-15T00:00:00Z"
    )
    zone = entries(data)["azure_openai/datazone.gpt-6-luna"]
    assert [str(e.effective_from) for e in zone] == ["2026-10-15", "2026-09-01"]


# --- output, the block and --check -------------------------------------------------------------


def test_the_output_is_deterministic_whatever_the_item_order() -> None:
    first = ap.render(ap.build(ap.read_items(items())), CHECKED)
    shuffled = items()
    shuffled.reverse()
    shuffled = shuffled[7:] + shuffled[:7]
    assert ap.render(ap.build(ap.read_items(shuffled)), CHECKED) == first
    assert first.startswith(ap.BEGIN + "\n") and first.endswith(ap.END + "\n")


def rendered(data: list[dict[str, Any]]) -> str:
    text: str = ap.render(ap.build(ap.read_items(data)), CHECKED)
    return text


def test_a_saved_download_reads_back_to_the_same_prices() -> None:
    data = items()
    again = ap.load_items(ap.dump_items(data))
    assert rendered(again) == rendered(data)
    assert isinstance(again[0]["retailPrice"], Decimal)


def catalog_copy(tmp_path: Path) -> Path:
    path = tmp_path / "prices.yaml"
    path.write_text((resources.files("spendgate.catalog") / "prices.yaml").read_text("utf-8"))
    return path


def run(*args: object) -> int:
    code: int = ap.main([str(a) for a in args])
    return code


def test_write_replaces_the_block_and_the_catalog_still_loads(tmp_path: Path) -> None:
    prices = catalog_copy(tmp_path)
    assert run("--from-file", DATA, "--write", "--catalog", prices, "--checked-on", CHECKED) == 0
    text = prices.read_text()
    assert text.count(ap.BEGIN) == 1 and text.count(ap.END) == 1
    catalog = parse([yaml.safe_load(text)], "0" * 64, ledger_currency="USD")
    sweden = catalog.lookup("azure_openai", "datazone.gpt-6-luna", NOW_MS, "swedencentral")
    assert sweden.meters.input_per_mtok == Decimal("0.12")
    assert catalog.lookup("azure_openai", "gpt-6-luna", NOW_MS).price_multiplier == Decimal("1.2")
    assert run("--from-file", DATA, "--check", "--catalog", prices) == 0


def test_check_ignores_checked_on_and_exits_1_on_a_price_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prices = catalog_copy(tmp_path)
    run("--from-file", DATA, "--write", "--catalog", prices, "--checked-on", "2026-01-01")
    assert run("--from-file", DATA, "--check", "--catalog", prices) == 0  # a later day: no drift
    data = items()
    item(data, "gpt 4.1 nano Inp regnl Tokens", "eastus2")["retailPrice"] = Decimal("0.000115")
    changed = tmp_path / "changed.json"
    changed.write_bytes(ap.dump_items(data))
    capsys.readouterr()
    assert run("--from-file", changed, "--check", "--catalog", prices) == 1
    assert "drift: azure_openai/regional.gpt-4.1-nano" in capsys.readouterr().err


def test_the_first_write_inserts_the_block_before_azure_speech() -> None:
    text = f"models:\n  - id: azure_openai/gpt-x\n\n{ap.ANCHOR}\n  - id: azure_speech/y\n"
    out = ap.replace_block(text, f"{ap.BEGIN}\n{ap.END}\n")
    assert out.index("azure_openai/gpt-x") < out.index(ap.BEGIN) < out.index(ap.ANCHOR)
    assert ap.replace_block(out, f"{ap.BEGIN}\n  # x\n{ap.END}\n").count(ap.BEGIN) == 1
    with pytest.raises(ap.ImportFailure):
        ap.replace_block(out + ap.BEGIN + "\n", "")
    assert ap.drift(text, ap.build(ap.read_items(items()))) == ["the generated block is missing"]


def test_bad_input_exits_2_without_touching_the_catalog(tmp_path: Path) -> None:
    prices = catalog_copy(tmp_path)
    before = prices.read_bytes()
    bad = tmp_path / "bad.json"
    bad.write_text('{"Items": "nope"}')
    assert run("--from-file", bad, "--write", "--catalog", prices) == 2
    assert prices.read_bytes() == before


# --- downloading (no network: a fake opener) ---------------------------------------------------


class FakeOpener:
    def __init__(self, pages: Mapping[str, bytes]) -> None:
        self.pages = pages
        self.asked: list[str] = []

    def open(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
        self.asked.append(request.full_url)
        return io.BytesIO(self.pages[request.full_url])


def page(data: list[dict[str, Any]], next_link: str | None) -> bytes:
    return json.dumps(
        {"Items": json.loads(ap.dump_items(data)), "NextPageLink": next_link}
    ).encode()


def test_download_follows_next_page_links_on_the_api_host_only() -> None:
    data = items()
    second = (
        "https://prices.azure.com:443/api/retail/prices?api-version=2023-01-01-preview&$skip=1000"
    )
    first = ap.first_page_url()
    opener = FakeOpener({first: page(data[:10], second), second: page(data[10:], None)})
    got = ap.download(opener)
    assert opener.asked == [first, second]
    assert len(got) == len(data)
    assert "serviceName%20eq%20%27Foundry%20Models%27" in first


@pytest.mark.parametrize(
    "link",
    [
        "http://prices.azure.com/api/retail/prices?$skip=1000",
        "https://prices.azure.com.example/api/retail/prices?$skip=1000",
        "https://example.com/api/retail/prices",
        "https://prices.azure.com/api/other",
        "https://prices.azure.com:8443/api/retail/prices",
    ],
)
def test_a_next_page_link_off_the_api_is_refused(link: str) -> None:
    opener = FakeOpener({ap.first_page_url(): page(items()[:3], link)})
    with pytest.raises(ap.ImportFailure, match="refusing a page outside"):
        ap.download(opener)


def test_download_caps_pages_and_page_size(monkeypatch: pytest.MonkeyPatch) -> None:
    first = ap.first_page_url()
    looping = FakeOpener({first: page(items()[:3], first)})
    monkeypatch.setattr(ap, "MAX_PAGES", 3)
    with pytest.raises(ap.ImportFailure, match="more than 3 pages"):
        ap.download(looping)
    monkeypatch.setattr(ap, "MAX_PAGE_BYTES", 100)
    with pytest.raises(ap.ImportFailure, match="larger than 100 bytes"):
        ap.download(FakeOpener({first: page(items(), None)}))


def test_redirects_are_refused() -> None:
    handler = ap._NoRedirects()
    request = urllib.request.Request(ap.first_page_url())  # noqa: S310 - never opened
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(request, None, 302, "Found", {}, "https://example.com/")
