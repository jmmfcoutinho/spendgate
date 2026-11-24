"""A bootstrap budget whose `model_filter` matches no routed catalog id is named at check-config.

The filter is matched against catalog ids (`openai/gpt-6-luna`), never aliases (`m`), so such a
budget would never take part in a hold. It is a warning, not an error: the config stays valid.
"""

from pathlib import Path

import pytest

from spendgate.cli import serve
from spendgate.config import check_config, load
from tests.unit.test_config import BASE, ENV, write


def with_filters(tmp_path: Path, *patterns: str) -> Path:
    rows = "".join(
        f"    - {{id: b{i}, scope: 'org:a', period: day, cap: '5 USD', model_filter: '{p}'}}\n"
        for i, p in enumerate(patterns)
    )
    text = BASE + "bootstrap:\n  scopes: ['org:a']\n  budgets:\n" + rows
    return write(tmp_path, text)


def test_a_filter_matching_a_routed_catalog_id_has_nothing_to_warn_about(tmp_path: Path) -> None:
    path = with_filters(tmp_path, "openai/gpt-6-luna", "openai/*", "*luna")
    assert load(path, env=ENV).unmatched_filters() == []
    assert check_config(path, env=ENV).unmatched_filters == ()


def test_a_filter_matching_no_routed_catalog_id_is_named_with_its_path(tmp_path: Path) -> None:
    path = with_filters(tmp_path, "openai/*", "m", "OPENAI/*", "openai/gpt-7-*")
    lines = load(path, env=ENV).unmatched_filters()
    assert [line.split(" ", 1)[0] for line in lines] == [
        "bootstrap.budgets.1.model_filter",  # the alias, not the catalog id
        "bootstrap.budgets.2.model_filter",  # case-sensitive
        "bootstrap.budgets.3.model_filter",  # matches no routed model
    ]
    assert "'m' matches no routed catalog id" in lines[0]
    assert "openai/gpt-6-luna" in lines[0] and "not aliases" in lines[0]


def test_check_config_prints_the_warning_and_stays_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    path = with_filters(tmp_path, "m")
    report = check_config(path, env=ENV)
    assert report.ok and len(report.unmatched_filters) == 1
    assert serve.run_check_config(str(path)) == 0
    captured = capsys.readouterr()
    assert "warning: bootstrap.budgets.0.model_filter 'm' matches no routed" in captured.err
    assert "configuration ok" in captured.out
