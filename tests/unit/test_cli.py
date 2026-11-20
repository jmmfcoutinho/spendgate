"""CLI pieces that need no ledger: parsing, amounts, durations, tables, exit code for usage."""

import pytest

from spendgate.cli import main
from spendgate.cli.client import EXIT_REFUSED, CliError
from spendgate.cli.commands import amount, until
from spendgate.cli.entry import build_parser
from spendgate.cli.render import money, table
from spendgate.cli.serve import Listen, parse_listen


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
