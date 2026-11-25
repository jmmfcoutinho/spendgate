"""`spendgate catalog import`: drafts a verified: false override file from LiteLLM's price file.

No network: the fixture copy in tests/data/litellm/ is read from disk, downloads go to a stub
fetcher or an httpx MockTransport, and the clock is passed in.
"""

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from spendgate.cli import catalog, main, serve
from spendgate.cli.client import EXIT_CONFIG, EXIT_OK, EXIT_UNAVAILABLE, EXIT_USAGE, CliError
from spendgate.cli.entry import build_parser
from spendgate.config import check_config
from spendgate.core import litellm
from tests.unit.test_config import ENV, write

FIXTURE = Path(__file__).resolve().parents[1] / "data" / "litellm" / "model_prices.json"
TODAY = date(2026, 11, 18)
SHA = "b" * 40
RAW = (
    f"https://raw.githubusercontent.com/BerriAI/litellm/{SHA}/model_prices_and_context_window.json"
)
# Models the bundled catalog does not carry, one per provider family the filter can pick.
EXTRA = {
    "gpt-q4-test": {
        "litellm_provider": "openai", "mode": "chat", "input_cost_per_token": 2e-06,
        "output_cost_per_token": 8e-06, "max_output_tokens": 32000,
    },
    "gemini/gemini-q4-test": {
        "litellm_provider": "gemini", "mode": "chat", "input_cost_per_token": 1e-06,
        "output_cost_per_token": 4e-06, "max_output_tokens": 8192,
    },
}  # fmt: skip


def run(*argv: str, fetcher: catalog.Fetch | None = None) -> int:
    args = build_parser().parse_args(["catalog", "import", *argv])
    try:
        if fetcher is None:
            return catalog.run_import(args, today=TODAY)
        return catalog.run_import(args, today=TODAY, fetcher=fetcher)
    except CliError as exc:
        return exc.code


def models(path: Path) -> list[dict[str, Any]]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    found: list[dict[str, Any]] = doc["models"]
    return found


def never(url: str) -> bytes:
    raise AssertionError(f"nothing may be downloaded here ({url})")


def test_the_fixture_becomes_unverified_entries_for_models_the_catalog_lacks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "extra.yaml"
    assert run("--from-file", str(FIXTURE), "--out", str(out), fetcher=never) == EXIT_OK
    entries = models(out)
    assert [e["id"] for e in entries] == ["openai/gpt-6-new"]  # the rest are bundled already
    assert all(e["verified"] is False for e in entries)
    assert entries[0]["effective_from"] == "2026-11-18" == entries[0]["checked_on"]
    assert entries[0]["meters"] == {"input_per_mtok": "3", "output_per_mtok": "15"}
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Drafted from LiteLLM") and "commit not named" in text
    captured = capsys.readouterr()
    assert "wrote 1 unverified entries" in captured.out


def test_m09_an_entry_without_an_output_ceiling_is_refused_never_defaulted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "extra.yaml"
    assert run("--from-file", str(FIXTURE), "--out", str(out)) == EXIT_OK
    assert "openai/gpt-6-nolimit" not in {e["id"] for e in models(out)}
    assert "skipped 'gpt-6-nolimit': no output ceiling" in capsys.readouterr().err
    data = litellm.parse_litellm(FIXTURE.read_bytes())
    mapped = litellm.map_key("gpt-6-nolimit", data["gpt-6-nolimit"])
    assert mapped is not None
    source = litellm.Source(None, "https://example.com/p", "0" * 64, data)
    entry = litellm.draft_entry(mapped, source, TODAY)
    assert "limits" not in entry  # nothing guessed in its place
    assert litellm.entry_problem(entry) == "no output ceiling (LiteLLM lists no max_output_tokens)"


def test_the_generated_file_passes_check_config_as_an_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    assert run("--from-file", str(FIXTURE), "--out", str(tmp_path / "extra.yaml")) == EXIT_OK
    config = write(
        tmp_path,
        'server: {listen: 127.0.0.1:8080, key_pepper: "env:SPENDGATE_KEY_PEPPER"}\n'
        "ledger: {currency: USD, backend: {kind: sqlite, path: ./l.db}}\n"
        'providers: {openai: {base_url: "https://api.openai.com/v1", '
        'credential: "env:OPENAI_API_KEY"}}\n'
        "models:\n  new: {catalog_id: openai/gpt-6-new, deployments: [{provider: openai}]}\n"
        "catalog: {paths: [extra.yaml]}\n",
    )
    report = check_config(config, env=ENV)
    assert report.ok, report.errors
    assert report.unverified is not None and "openai/gpt-6-new@2026-11-18" in report.unverified
    capsys.readouterr()
    assert serve.run_check_config(str(config)) == EXIT_OK
    assert "configuration ok" in capsys.readouterr().out


def test_an_existing_file_is_never_overwritten_without_force(tmp_path: Path) -> None:
    out = tmp_path / "extra.yaml"
    out.write_text("# reviewed by hand\n", encoding="utf-8")
    assert run("--from-file", str(FIXTURE), "--out", str(out), fetcher=never) == EXIT_USAGE
    assert out.read_text(encoding="utf-8") == "# reviewed by hand\n"
    assert run("--from-file", str(FIXTURE), "--out", str(out), "--force") == EXIT_OK
    assert [e["id"] for e in models(out)] == ["openai/gpt-6-new"]


def test_the_open_refuses_a_file_that_appeared_after_the_check(tmp_path: Path) -> None:
    out = tmp_path / "extra.yaml"

    def racing(url: str) -> bytes:
        out.write_text("# written meanwhile\n", encoding="utf-8")
        return FIXTURE.read_bytes()

    assert run("--from-litellm", SHA, "--out", str(out), fetcher=racing) == EXIT_USAGE
    assert out.read_text(encoding="utf-8") == "# written meanwhile\n"


def test_the_commit_must_be_a_full_sha_never_a_branch(tmp_path: Path) -> None:
    out = tmp_path / "extra.yaml"
    for bad in ("main", "v1.2.3", SHA[:12], SHA.upper(), SHA + "b"):
        assert run("--from-litellm", bad, "--out", str(out), fetcher=never) == EXIT_USAGE
    assert run("--out", str(out), fetcher=never) == EXIT_USAGE  # no source at all
    assert not out.exists()


def test_a_commit_is_fetched_from_the_pinned_raw_url_and_recorded(tmp_path: Path) -> None:
    seen: list[str] = []

    def fetcher(url: str) -> bytes:
        seen.append(url)
        return FIXTURE.read_bytes()

    out = tmp_path / "extra.yaml"
    assert run("--from-litellm", SHA, "--out", str(out), fetcher=fetcher) == EXIT_OK
    assert seen == [RAW]
    (entry,) = models(out)
    assert entry["source_url"] == RAW and SHA[:12] in entry["note"]
    assert f"BerriAI/litellm @ {SHA}" in out.read_text(encoding="utf-8")


def test_a_local_copy_of_a_named_commit_is_read_not_fetched(tmp_path: Path) -> None:
    out = tmp_path / "extra.yaml"
    args = ("--from-litellm", SHA, "--from-file", str(FIXTURE), "--out", str(out))
    assert run(*args, fetcher=never) == EXIT_OK
    assert models(out)[0]["source_url"] == RAW


def test_providers_limits_the_output_and_unknown_slugs_are_refused(tmp_path: Path) -> None:
    src = tmp_path / "prices.json"
    src.write_text(json.dumps(EXTRA), encoding="utf-8")
    both, google = tmp_path / "both.yaml", tmp_path / "google.yaml"
    assert run("--from-file", str(src), "--out", str(both)) == EXIT_OK
    assert {e["id"] for e in models(both)} == {"openai/gpt-q4-test", "google/gemini-q4-test"}
    assert run("--from-file", str(src), "--out", str(google), "--providers", "google") == EXIT_OK
    assert [e["id"] for e in models(google)] == ["google/gemini-q4-test"]
    bad = tmp_path / "bad.yaml"
    assert run("--from-file", str(src), "--out", str(bad), "--providers", "mistral") == EXIT_USAGE
    assert run("--from-file", str(src), "--out", str(bad), "--providers", " , ") == EXIT_USAGE
    assert not bad.exists()


def test_nothing_to_draft_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "extra.yaml"
    assert run("--from-file", str(FIXTURE), "--out", str(out), "--providers", "anthropic") == 3
    assert not out.exists()


@pytest.mark.parametrize("body", [b"404: Not Found", b"[1, 2]", b"{"])
def test_a_file_that_is_not_litellm_json_is_refused(tmp_path: Path, body: bytes) -> None:
    src = tmp_path / "prices.json"
    src.write_bytes(body)
    out = tmp_path / "extra.yaml"
    assert run("--from-file", str(src), "--out", str(out)) == EXIT_CONFIG
    assert run("--from-file", str(tmp_path / "missing.json"), "--out", str(out)) == EXIT_CONFIG
    assert not out.exists()


def test_an_oversized_file_is_refused_before_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(litellm, "MAX_BYTES", 10)
    out = tmp_path / "extra.yaml"
    assert run("--from-file", str(FIXTURE), "--out", str(out)) == EXIT_CONFIG
    assert not out.exists()


def test_main_runs_the_command_without_a_config_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no spendgate.yaml here, and none is needed
    assert main(["catalog", "import", "--from-file", str(FIXTURE), "--out", "x.yaml"]) == 0
    assert main(["catalog", "import", "--from-file", str(FIXTURE), "--out", "x.yaml"]) == 2
    assert "pass --force" in capsys.readouterr().err


# --- the download ----------------------------------------------------------------------------


def mock(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    return httpx.MockTransport(record), calls


def test_fetch_reads_the_body_over_https_from_the_raw_host() -> None:
    transport, calls = mock(lambda r: httpx.Response(200, content=b"{}"))
    assert catalog.fetch(RAW, transport) == b"{}"
    assert [str(c.url) for c in calls] == [RAW]


@pytest.mark.parametrize(
    "url",
    [
        RAW.replace("https://", "http://"),
        RAW.replace("raw.githubusercontent.com", "example.com"),
        RAW.replace("raw.githubusercontent.com", "raw.githubusercontent.com.example.com"),
    ],
)
def test_fetch_refuses_any_other_scheme_or_host(url: str) -> None:
    transport, calls = mock(lambda r: httpx.Response(200, content=b"{}"))
    with pytest.raises(CliError) as exc:
        catalog.fetch(url, transport)
    assert exc.value.code == EXIT_USAGE and not calls


def test_fetch_does_not_follow_a_redirect() -> None:
    transport, calls = mock(
        lambda r: httpx.Response(302, headers={"Location": "https://example.com/prices.json"})
    )
    with pytest.raises(CliError) as exc:
        catalog.fetch(RAW, transport)
    assert exc.value.code == EXIT_UNAVAILABLE and len(calls) == 1


def test_fetch_stops_reading_past_the_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(litellm, "MAX_BYTES", 4)
    transport, _ = mock(lambda r: httpx.Response(200, content=b"0123456789"))
    with pytest.raises(CliError) as exc:
        catalog.fetch(RAW, transport)
    assert exc.value.code == EXIT_CONFIG


def test_a_transport_error_is_unavailable() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(CliError) as exc:
        catalog.fetch(RAW, httpx.MockTransport(fail))
    assert exc.value.code == EXIT_UNAVAILABLE


def test_a_draft_that_would_not_load_is_named_without_its_values() -> None:
    entry = {
        "id": "openai/x", "mode": "embedding", "currency": "USD", "effective_from": "2026-11-18",
        "source_url": "https://example.com/p", "verified": False, "meters": {"per_image": "0.5"},
    }  # fmt: skip
    problem = litellm.entry_problem(entry)
    assert problem is not None and "embedding needs input_per_mtok" in problem
    assert "0.5" not in problem


def test_the_mapper_refuses_an_oversized_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(litellm, "MAX_BYTES", 1)
    with pytest.raises(litellm.LiteLLMError, match="larger than"):
        litellm.parse_litellm(b"{}")


# --- hostile input ---------------------------------------------------------------------------

GOOD = (
    '"ok-model": {"litellm_provider": "openai", "mode": "chat", "input_cost_per_token": 1e-06,'
    ' "output_cost_per_token": 2e-06, "max_output_tokens": 1000}'
)


def chat(key: str, **fields: object) -> str:
    spec: dict[str, object] = {
        "litellm_provider": "openai", "mode": "chat", "input_cost_per_token": 1e-06,
        "output_cost_per_token": 2e-06, "max_output_tokens": 1000,
    }  # fmt: skip
    spec.update(fields)
    return f"{json.dumps(key)}: {json.dumps(spec)}"


def import_text(
    tmp_path: Path, body: str, capsys: pytest.CaptureFixture[str]
) -> tuple[int, list[str], str]:
    src = tmp_path / "prices.json"
    src.write_text("{" + body + "}", encoding="utf-8")
    out = tmp_path / "extra.yaml"
    out.unlink(missing_ok=True)
    rc = run("--from-file", str(src), "--out", str(out))
    ids = [e["id"] for e in models(out)] if out.exists() else []
    return rc, ids, capsys.readouterr().err


def test_one_bad_value_skips_that_entry_and_keeps_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = (
        '"m-nan": {"litellm_provider": "openai", "mode": "chat", "input_cost_per_token": NaN,'
        ' "output_cost_per_token": 1e-06, "max_output_tokens": 10},'
        '"m-big": {"litellm_provider": "openai", "mode": "chat",'
        ' "input_cost_per_token": 1e999990, "output_cost_per_token": 1e-06,'
        ' "max_output_tokens": 10},'
    )
    body = raw + ",".join(
        (
            chat("m-str", input_cost_per_token="0.000001"),  # noqa: S106 - a price, not a secret
            chat("m-bool", output_cost_per_token=True),
        )
    )
    rc, ids, err = import_text(tmp_path, body + "," + GOOD, capsys)
    assert rc == EXIT_OK and ids == ["openai/ok-model"]
    for key in ("m-nan", "m-big", "m-str", "m-bool"):
        assert f"skipped '{key}': a price or limit is not a usable finite number" in err
    assert "0.000001" not in err and "True" not in err and "999990" not in err


def test_a_bad_value_inside_the_mapper_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> litellm.Drafts:
        raise ArithmeticError

    monkeypatch.setattr(litellm, "draft_overrides", broken)
    assert run("--from-file", str(FIXTURE), "--out", str(tmp_path / "x.yaml")) == EXIT_CONFIG


def test_m10_a_zero_price_from_litellm_is_never_drafted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = ",".join(
        (
            chat("m-free", input_cost_per_token=0, output_cost_per_token=0),
            chat("m-free-out", output_cost_per_token=0.0),
            chat("m-free-reasoning", output_cost_per_reasoning_token=0),
            chat("m-free-cache", cache_read_input_token_cost=0),
            TINY,
            GOOD,
        )
    )
    rc, ids, err = import_text(tmp_path, body, capsys)
    assert rc == EXIT_OK and ids == ["openai/ok-model"]
    assert "skipped 'm-free': zero price for input_per_mtok" in err
    assert "skipped 'm-free-out': zero price for output_per_mtok" in err
    assert "skipped 'm-free-reasoning': zero price for reasoning_per_mtok" in err
    assert "skipped 'm-free-cache': zero price for cached_input_per_mtok" in err
    assert "skipped 'm-tiny': zero price for input_per_mtok" in err  # underflows to 0


# Below Decimal's smallest exponent: the conversion underflows to 0, which must not pass as free.
TINY = (
    '"m-tiny": {"litellm_provider": "openai", "mode": "chat",'
    ' "input_cost_per_token": 1e-999999999, "output_cost_per_token": 1e-06,'
    ' "max_output_tokens": 10}'
)


def test_m10_an_embedding_drops_the_zero_output_meter_it_never_bills(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = {
        "litellm_provider": "openai", "mode": "embedding", "input_cost_per_token": 1e-08,
        "output_cost_per_token": 0.0,
    }  # fmt: skip
    rc, ids, _ = import_text(tmp_path, f'"emb-q4-test": {json.dumps(spec)}', capsys)
    assert rc == EXIT_OK and ids == ["openai/emb-q4-test"]
    assert models(tmp_path / "extra.yaml")[0]["meters"] == {"input_per_mtok": "0.01"}


def test_m10_a_long_context_discount_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = chat("m-lc", input_cost_per_token_above_200k_tokens=5e-07) + "," + GOOD
    rc, ids, err = import_text(tmp_path, body, capsys)
    assert rc == EXIT_OK and ids == ["openai/ok-model"]
    assert "skipped 'm-lc': long-context input_multiplier below 1" in err


def test_m09_a_fractional_output_ceiling_is_refused_never_rounded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, ids, err = import_text(tmp_path, chat("m-frac", max_output_tokens=4096.9), capsys)
    assert rc == EXIT_CONFIG and ids == []
    assert "skipped 'm-frac': a price or limit is not a usable finite number" in err
    assert litellm.whole(4096) == 4096
    with pytest.raises(litellm.LiteLLMError, match="whole"):
        litellm.whole(litellm.to_decimal(litellm.parse_litellm(b'{"x": {"n": 1.5}}')["x"]["n"]))


def test_a_skipped_key_cannot_drive_the_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = chat("\x1b]0;pwned\x07\x1b[2J-x") + "," + GOOD
    rc, _, err = import_text(tmp_path, body, capsys)
    assert rc == EXIT_OK
    assert "\x1b" not in err and "\x07" not in err and "\\x1b]0;pwned" in err


@pytest.mark.skipif(not Path("/dev/zero").exists(), reason="needs /dev/zero")
def test_from_file_reads_no_more_than_the_cap_from_an_endless_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(litellm, "MAX_BYTES", 1024)  # stat says 0 bytes; the read must stop
    assert run("--from-file", "/dev/zero", "--out", str(tmp_path / "x.yaml")) == EXIT_CONFIG


def test_force_replaces_the_file_in_one_step_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "extra.yaml"
    out.write_text("# old\n", encoding="utf-8")
    real = Path.replace

    def crash(self: Path, target: Any) -> Path:  # noqa: ANN401 - mirrors Path.replace
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "replace", crash)
    assert run("--from-file", str(FIXTURE), "--out", str(out), "--force") == EXIT_CONFIG
    assert out.read_text(encoding="utf-8") == "# old\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["extra.yaml"]
    monkeypatch.setattr(Path, "replace", real)
    assert run("--from-file", str(FIXTURE), "--out", str(out), "--force") == EXIT_OK
    assert sorted(p.name for p in tmp_path.iterdir()) == ["extra.yaml"]
    assert models(out)[0]["id"] == "openai/gpt-6-new"
