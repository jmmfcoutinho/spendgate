"""The config `bootstrap` section: create-only, idempotent, secrets only when created."""

import re
from pathlib import Path

import pytest

from spendgate.cli import serve
from spendgate.cli.runtime import open_runtime
from spendgate.config import Settings, load
from spendgate.core.errors import Refusal
from spendgate.core.sessions import SessionDefaults
from spendgate.gateway.bootstrap import apply_bootstrap
from spendgate.ledger.key_types import AdminScope, Principal
from spendgate.ledger.keys import KeyService
from spendgate.ledger.sqlite import SqliteLedger
from tests.integration.harness import (
    CATALOG_OVERRIDE,
    CONFIG,
    ENV,
    MODELS,
    NOW_MS,
    PEPPER,
    async_test,
)

BOOTSTRAP = """\
bootstrap:
  scopes: ["org:acme/project:app"]
  budgets:
    - {id: monthly, scope: "org:acme/project:app", period: month, cap: "40 USD", soft_pct: 70}
  keys:
    - {name: app-key, scope: "org:acme/project:app", models: [alpha], limits: {rpm: 10}}
    - {name: ops, admin_scopes: ["usage:read", "keys:write"]}
"""


@async_test
async def test_bootstrap_creates_once_and_never_changes_existing_rows(tmp_path: Path) -> None:
    text = CONFIG.format(
        max_body=1_000_000, max_json_body=1_000_000, concurrent=8, attempts=2, cooldown=5,
        models=MODELS,
    )  # fmt: skip
    (tmp_path / "spendgate.yaml").write_text(text + BOOTSTRAP)
    (tmp_path / "catalog_override.yaml").write_text(CATALOG_OVERRIDE)
    settings = load(tmp_path / "spendgate.yaml", env=ENV)
    assert settings.ledger_path is not None
    ledger = SqliteLedger.open(settings.ledger_path, currency="USD")
    try:
        keys = KeyService(ledger, PEPPER)
        first = await apply_bootstrap(settings, ledger, keys, NOW_MS)
        assert first.scopes == ["org:acme", "org:acme/project:app", "org:spendgate-admin"]
        assert first.budgets == ["monthly"]
        assert [(k.name, k.kind) for k in first.keys] == [("app-key", "api"), ("ops", "admin")]
        secrets = {k.name: k.secret for k in first.keys}
        assert isinstance(await keys.verify_api(secrets["app-key"], NOW_MS), Principal)
        admin = await keys.verify_admin(secrets["ops"], NOW_MS, AdminScope.KEYS_WRITE)
        assert isinstance(admin, Principal)
        refused = await keys.verify_admin(secrets["ops"], NOW_MS, AdminScope.BUDGETS_WRITE)
        assert isinstance(refused, Refusal)  # only the scopes the config named

        await ledger.update_budget("bootstrap-monthly", actor="op", now=NOW_MS, cap_nano=1)
        again = await apply_bootstrap(settings, ledger, keys, NOW_MS + 1)
        assert (again.scopes, again.budgets, again.keys) == ([], [], [])  # no secrets re-issued
        view = await ledger.get_budget("bootstrap-monthly", NOW_MS)
        assert view is not None and view.budget.cap_nano == 1  # an operator's change survives
        assert view.note == "bootstrap:monthly" and view.budget.soft_pct == 70
        assert len(await ledger.list_keys(None)) == 1
        actions = [
            e.actor
            for e in await ledger.list_audit(since=0, until=None, actor=None, before=None, limit=99)
        ]
        assert actions.count("bootstrap") >= 5 and "op" in actions
    finally:
        ledger.close()


async def _settings(tmp_path: Path, extra: str = BOOTSTRAP) -> Settings:
    text = CONFIG.format(
        max_body=1_000_000, max_json_body=1_000_000, concurrent=8, attempts=2, cooldown=5,
        models=MODELS,
    )  # fmt: skip
    (tmp_path / "spendgate.yaml").write_text(text + extra)
    (tmp_path / "catalog_override.yaml").write_text(CATALOG_OVERRIDE)
    return load(tmp_path / "spendgate.yaml", env=ENV)


# T-LOG-04
@async_test
async def test_t_log_04_serve_bootstraps_scopes_and_budgets_but_never_prints_or_creates_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SR-08: a secret printed by `serve` ends up in the container log."""
    settings = await _settings(tmp_path)

    async def no_listener(*_args: object) -> None:
        return None

    monkeypatch.setattr(serve, "_run", no_listener)
    async with open_runtime(settings) as rt:
        await serve._serve(rt, bootstrap=True)
        assert await rt.ledger.list_keys(None) == ()  # no key was created
        assert await rt.ledger.find_scope("org:acme/project:app") is not None
        assert await rt.ledger.get_budget("bootstrap-monthly", NOW_MS) is not None
    err = capsys.readouterr().err
    assert not re.search(r"\bsga?-[0-9a-f]{12}-[0-9a-f]{48}\b", err)
    assert "migrate --bootstrap" in err  # says where keys come from


@async_test
async def test_t_log_04_migrate_bootstrap_still_creates_and_prints_each_key_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = await _settings(tmp_path)
    await serve._migrate(settings, bootstrap=True)
    secrets = re.findall(r"\bsga?-[0-9a-f]{12}-[0-9a-f]{48}\b", capsys.readouterr().err)
    assert len(secrets) == 2 and len(set(secrets)) == 2


# T-KEY-01
@async_test
async def test_t_key_01_a_bootstrap_key_with_empty_models_is_refused(tmp_path: Path) -> None:
    empty = BOOTSTRAP.replace("models: [alpha]", "models: []")
    settings = await _settings(tmp_path, empty)
    ledger = SqliteLedger.open(settings.ledger_path, currency="USD")  # type: ignore[arg-type]
    try:
        with pytest.raises(ValueError, match="models must not be empty"):
            await apply_bootstrap(settings, ledger, KeyService(ledger, PEPPER), NOW_MS)
        assert await ledger.list_keys(None) == ()
    finally:
        ledger.close()


# T-SESS-09
@async_test
async def test_m11_a_bootstrap_agent_key_stores_session_required_and_the_new_session_bound(
    tmp_path: Path,
) -> None:
    agent = BOOTSTRAP.replace(
        "limits: {rpm: 10}",
        "limits: {rpm: 10}, session_required: true, session_defaults: {max_steps: 5}",
    )
    settings = await _settings(tmp_path, agent)
    ledger = SqliteLedger.open(settings.ledger_path, currency="USD")  # type: ignore[arg-type]
    try:
        keys = KeyService(ledger, PEPPER)
        await apply_bootstrap(settings, ledger, keys, NOW_MS)
        (key,) = await ledger.list_keys(None)
        assert key.policy.session_defaults == SessionDefaults(
            max_steps=5, max_new_sessions_per_hour=60, required=True
        )
        again = await apply_bootstrap(settings, ledger, keys, NOW_MS + 1)
        assert again.warnings == []  # the stored key matches the config: nothing to report
    finally:
        ledger.close()


@async_test
async def test_bootstrap_warns_when_an_existing_key_has_other_session_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = await _settings(tmp_path)
    ledger = SqliteLedger.open(settings.ledger_path, currency="USD")  # type: ignore[arg-type]
    try:
        keys = KeyService(ledger, PEPPER)
        await apply_bootstrap(settings, ledger, keys, NOW_MS)
        same = await apply_bootstrap(settings, ledger, keys, NOW_MS + 1)
        assert same.warnings == []  # unchanged config: nothing to report
        agent = BOOTSTRAP.replace(
            "limits: {rpm: 10}",
            "limits: {rpm: 10}, session_required: true, session_defaults: {max_steps: 5}",
        )
        changed = await _settings(tmp_path, agent)
        report = await apply_bootstrap(changed, ledger, keys, NOW_MS + 2)
        (warning,) = report.warnings
        assert "app-key" in warning and "PATCH /admin/v1/keys/" in warning
        api = [k for k in await ledger.list_keys(None) if k.name == "app-key"]
        assert api[0].policy.session_defaults is None  # create-only: never applied
        serve.print_bootstrap(report)
        assert "warning: bootstrap: key app-key" in capsys.readouterr().err
    finally:
        ledger.close()
