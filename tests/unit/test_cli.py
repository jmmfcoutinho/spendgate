"""CLI pieces that need no ledger: parsing, amounts, durations, tables, exit code for usage."""

import argparse
import importlib.metadata
from pathlib import Path

import pytest

from spendgate import package_version
from spendgate.cli import main
from spendgate.cli.client import EXIT_REFUSED, CliError
from spendgate.cli.commands import amount, until
from spendgate.cli.entry import admin_url, build_parser, resolve_defaults
from spendgate.cli.render import money, table
from spendgate.cli.serve import Listen, parse_listen, run_check_config
from spendgate.gateway.admin_http import AdminContext

CONFIG = """\
server: {{key_pepper: "env:SPENDGATE_KEY_PEPPER", {server}}}
ledger: {{currency: USD, backend: {{kind: sqlite, path: ./l.db}}}}
providers: {{openai: {{base_url: "https://api.openai.com/v1", credential: "env:FAKE_OPENAI_KEY"}}}}
models:
    m: {{catalog_id: openai/gpt-6-luna, deployments: [{{provider: openai}}, {{provider: openai}}]}}
"""


def test_every_documented_command_parses() -> None:
    p = build_parser()
    for argv in (
        ["serve", "--no-bootstrap"], ["check-config"], ["migrate", "--bootstrap"],
        ["keys", "create", "--scope", "org:a", "--models", "m1,m2", "--rpm", "5"],
        ["keys", "list", "--include-revoked"], ["keys", "revoke", "k"], ["keys", "rotate", "k"],
        ["budgets", "set", "org:a", "--cap", "5 EUR"], ["budgets", "list", "org:a"],
        ["budgets", "grant", "b", "--amount", "1 EUR", "--until", "7d", "--reason", "r"],
        ["budgets", "ack-overrun", "b", "--reason", "r"], ["kill", "on", "--reason", "r"],
        ["kill", "on", "--model", "openai/x", "--reason", "r"], ["kill", "off", "id"],
        ["usage", "--by", "service,model", "--since", "7d", "--json"], ["status", "--local"],
    ):  # fmt: skip
        assert p.parse_args(argv).command == argv[0]


def test_a_bad_command_line_exits_with_2_and_the_admin_key_has_no_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["keys", "create"])  # --scope is required
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["status", "--admin-key", "sga-x"])
    assert exc.value.code == 2
    capsys.readouterr()


def test_amount_parses_to_a_money_object_and_refuses_garbage() -> None:
    assert amount("12.5 eur") == {"amount": "12.500000000", "currency": "EUR"}
    with pytest.raises(CliError) as exc:
        amount("twelve")
    assert exc.value.code == EXIT_REFUSED


def test_until_turns_a_duration_into_a_time_and_passes_a_time_through() -> None:
    assert until("2031-01-01T00:00:00Z") == "2031-01-01T00:00:00Z"
    assert until("7d").endswith("Z") and until("7d") > "2026"


def test_table_aligns_columns_and_money_prints_amount_and_currency() -> None:
    text = table(("A", "BB"), [("long-cell", 1), ("x", 22)])
    assert text.splitlines() == ["A          BB", "long-cell  1", "x          22"]
    assert table(("ONLY",), []) == "ONLY"
    assert money({"amount": "1.000000000", "currency": "EUR"}) == "1.000000000 EUR"


def test_listen_addresses_split_host_and_port() -> None:
    assert parse_listen("127.0.0.1:8080") == Listen("127.0.0.1", 8080)
    assert parse_listen("[::1]:9") == Listen("::1", 9)


def _args(*argv: str) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    resolve_defaults(args)
    return args


def test_version_flag_prints_the_installed_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"spendgate {importlib.metadata.version('spendgate')}"


def test_the_admin_context_reports_the_installed_version_by_default() -> None:
    assert package_version() == importlib.metadata.version("spendgate") != "0.0.0"
    made = AdminContext(None, None, None, None, 0)  # type: ignore[arg-type]
    assert made.version == package_version()


@pytest.mark.parametrize("command", ["check-config", "serve", "migrate"])
def test_process_commands_take_only_config_and_not_admin_flags(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main([command, "--help"])
    shown = capsys.readouterr().out
    assert "--config" in shown
    assert not {"--url", "--local", "--json"} & set(shown.replace("[", " ").split())
    for flag in ("--url", "--local", "--json"):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args([command, flag, *(["x"] if flag == "--url" else [])])
        assert exc.value.code == 2
    capsys.readouterr()


def test_admin_url_defaults_to_the_admin_listener_in_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SPENDGATE_URL", raising=False)
    monkeypatch.delenv("SPENDGATE_CONFIG", raising=False)
    path = tmp_path / "spendgate.yaml"
    for server, url in (
        ("listen: 127.0.0.1:8080", "http://127.0.0.1:8080"),  # the README: one shared port
        ("listen: 0.0.0.0:7000", "http://127.0.0.1:7000"),
        ("listen: '[::]:7000'", "http://[::1]:7000"),
        ("listen: 0.0.0.0:80, admin: {listen: 127.0.0.1:9091}", "http://127.0.0.1:9091"),
    ):
        path.write_text(CONFIG.format(server=server), encoding="utf-8")
        assert admin_url(_args("status", "--config", str(path))) == url
    assert admin_url(_args("status", "--config", str(path), "--url", "http://h:1")) == "http://h:1"
    monkeypatch.setenv("SPENDGATE_URL", "http://env:2")
    assert admin_url(_args("status", "--config", str(path))) == "http://env:2"


def test_admin_url_without_a_config_falls_back_unless_one_was_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SPENDGATE_URL", raising=False)
    monkeypatch.delenv("SPENDGATE_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # no spendgate.yaml here
    assert admin_url(_args("status")) == "http://127.0.0.1:8080"
    assert admin_url(_args("status", "--config", "missing.yaml")) == 3
    assert "missing.yaml" in capsys.readouterr().err


def test_hints_repeat_the_flags_the_user_typed() -> None:
    assert _args("kill", "on", "--reason", "r").again == ""
    local = _args("kill", "on", "--local", "--config", "my conf.yaml", "--reason", "r")
    assert local.again == " --local --config 'my conf.yaml'"
    assert _args("kill", "on", "--url", "http://h:1", "--reason", "r").again == " --url http://h:1"


def test_check_config_prints_each_error_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SPENDGATE_KEY_PEPPER", "pepper-value")
    monkeypatch.delenv("FAKE_OPENAI_KEY", raising=False)
    path = tmp_path / "spendgate.yaml"
    unpriced = "    u: {catalog_id: openai/gpt-0-nothing, deployments: [{provider: openai}]}\n"
    path.write_text(CONFIG.format(server="listen: 127.0.0.1:8080") + unpriced, encoding="utf-8")
    assert run_check_config(str(path)) == 3
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == len(set(lines)) == 2  # three deployments share one missing credential
    assert all(line.startswith("error: ") for line in lines)
    assert any("gpt-0-nothing" in line for line in lines)
