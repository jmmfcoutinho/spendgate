"""spendgate.yaml loading: valid examples, and every invalid case names its path."""

import os
import re
import textwrap
from pathlib import Path

import pytest
import yaml

from spendgate.config import ConfigError, Secret, check_config, load
from spendgate.config.schema import SessionDefaultsSpec
from spendgate.config.secrets import resolve
from spendgate.config.yamlio import StrictLoader

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
SECRET = "sk-test-DO-NOT-LEAK-0123456789"  # noqa: S105  # pragma: allowlist secret
ENV = {
    "SPENDGATE_KEY_PEPPER": "pepper-value-fake-0123",
    "OPENAI_API_KEY": SECRET,
    "SPENDGATE_DATABASE_URL": "postgresql://u:pw-secret@db/spend",  # pragma: allowlist secret
    "AZURE_OPENAI_API_KEY": SECRET,
}

BASE = """\
server: {listen: 127.0.0.1:8080, key_pepper: "env:SPENDGATE_KEY_PEPPER"}
ledger: {currency: USD, backend: {kind: sqlite, path: ./l.db}}
providers: {openai: {base_url: "https://api.openai.com/v1", credential: "env:OPENAI_API_KEY"}}
models:
  m: {catalog_id: openai/gpt-6-luna, deployments: [{provider: openai}]}
"""


def write(tmp_path: Path, text: str = BASE, name: str = "spendgate.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def example(tmp_path: Path, name: str) -> Path:
    """Copy an example, pointing its `file:` references at real files under tmp_path."""
    text = (EXAMPLES / name).read_text(encoding="utf-8")
    secret = tmp_path / "secret"
    secret.write_text("file-secret-fake-0123\n", encoding="utf-8")
    return write(tmp_path, re.sub(r"file:/run/secrets/\w+", f"file:{secret}", text), name)


def failure(path: Path, env: dict[str, str] | None = None) -> str:
    with pytest.raises(ConfigError) as info:
        load(path, env=ENV if env is None else env)
    return str(info.value)


def test_examples_load_and_check(tmp_path: Path) -> None:
    sqlite = load(example(tmp_path, "config.yaml"), env=ENV)
    assert sqlite.ledger_currency == "EUR"
    assert sqlite.routes["gpt-6-luna"].deployments[0].credential == Secret(SECRET)
    assert sqlite.routes["claude-haiku"].deployments[0].credential == Secret(
        "file-secret-fake-0123"
    )
    assert sqlite.heartbeat_ms == 20_000 and sqlite.lease_ms == 60_000
    assert sqlite.config.bootstrap.budgets[0].cap.nano == 200 * 10**9
    agent = next(k for k in sqlite.config.bootstrap.keys if k.name == "agent-key")
    assert agent.session_required and agent.limits.concurrency  # the agent example is bounded
    postgres = load(example(tmp_path, "config.postgres.yaml"), env=ENV)
    assert postgres.ledger_dsn is not None and postgres.routes["gpt-6-luna"].deployments[1].region
    assert postgres.routes["gpt-6-luna"].deployments[0].model == "alpha-prod"
    for name in ("config.yaml", "config.postgres.yaml"):
        report = check_config(example(tmp_path, name), env=ENV)
        assert report.ok and report.unpriced == ()


def test_catalog_sha_is_stable_and_covers_overrides(tmp_path: Path) -> None:
    plain = load(write(tmp_path), env=ENV)
    assert plain.catalog_sha256 == plain.catalog.sha256 and len(plain.catalog_sha256) == 64
    override = tmp_path / "o.yaml"
    override.write_text(
        "catalog_version: 1\nmodels:\n  - {id: openai/gpt-6-luna, mode: chat, currency: USD,"
        ' effective_from: "2026-12-01", source_url: "https://x.example/p", verified: false,'
        ' meters: {input_per_mtok: "1", output_per_mtok: "2"}, limits: {max_output_tokens: 10}}\n',
        encoding="utf-8",
    )
    with_override = load(write(tmp_path, BASE + "catalog: {paths: [o.yaml]}\n"), env=ENV)
    assert with_override.catalog_sha256 != plain.catalog_sha256


def test_defaults_come_from_the_docs(tmp_path: Path) -> None:
    s = load(write(tmp_path), env=ENV)
    c = s.config
    assert (c.routing.max_attempts, c.routing.cooldown_seconds) == (2, 5)
    assert (c.defaults.max_embedding_items, c.ledger.retention_days) == (256, 90)
    assert (c.ledger.sweep_seconds, s.sweep_ms) == (30, 30_000)  # server sweeper interval
    assert c.anthropic.allowed_versions == ["2023-06-01", "2026-10-21"]
    assert c.anthropic.allowed_betas == []
    assert s.health.cooldown_ms == 5_000
    assert s.fail_open_summary() == ["fail_open: no opt-outs enabled"]


def test_concurrency_has_a_finite_default_and_null_is_the_explicit_opt_out(
    tmp_path: Path,
) -> None:
    assert load(write(tmp_path), env=ENV).config.defaults.limits.concurrency == 16
    rpm_only = load(write(tmp_path, BASE + "defaults: {limits: {rpm: 5}}\n"), env=ENV)
    assert rpm_only.config.defaults.limits.concurrency == 16  # setting one limit keeps the rest
    opted_out = BASE + "defaults: {limits: {concurrency: null}}\n"
    assert load(write(tmp_path, opted_out), env=ENV).config.defaults.limits.concurrency is None


AGENT_KEY = "bootstrap:\n  scopes: ['org:a']\n  keys: [{name: k, scope: 'org:a', %s}]\n"


def test_m11_session_defaults_bound_new_sessions_per_hour_unless_null(tmp_path: Path) -> None:
    def sessions_of(fields: str) -> SessionDefaultsSpec:
        key = load(write(tmp_path, BASE + AGENT_KEY % fields), env=ENV).config.bootstrap.keys[0]
        assert key.session_defaults is not None and key.session_required is False
        return key.session_defaults

    assert sessions_of("session_defaults: {max_steps: 5}").max_new_sessions_per_hour == 60
    unbounded = sessions_of("session_defaults: {max_new_sessions_per_hour: null}")
    assert unbounded.max_new_sessions_per_hour is None


def test_session_required_needs_session_defaults(tmp_path: Path) -> None:
    assert "session_required needs session_defaults" in failure(
        write(tmp_path, BASE + AGENT_KEY % "session_required: true")
    )
    fields = "session_required: true, session_defaults: {max_steps: 5}"
    key = load(write(tmp_path, BASE + AGENT_KEY % fields), env=ENV).config.bootstrap.keys[0]
    assert key.session_required is True


def test_tracing_is_off_by_default_and_its_keys_are_checked(tmp_path: Path) -> None:
    otel = load(write(tmp_path), env=ENV).config.observability.otel
    assert (otel.enabled, otel.service_name) == (False, "spendgate")
    on = BASE + "observability: {otel: {enabled: true, service_name: gw-eu}}\n"
    assert load(write(tmp_path, on), env=ENV).config.observability.otel.service_name == "gw-eu"
    typo = BASE + "observability: {otel: {enabled: true, endpoint: http://x}}\n"
    assert "observability.otel.endpoint" in failure(write(tmp_path, typo))


def test_fail_open_opt_out_is_listed(tmp_path: Path) -> None:
    s = load(write(tmp_path, BASE + "fail_open: {allow_remote_media: true}\n"), env=ENV)
    (line,) = s.fail_open_summary()
    assert "fail_open.allow_remote_media is ON" in line


def test_unknown_key_names_the_path(tmp_path: Path) -> None:
    message = failure(write(tmp_path, BASE + "routing: {max_atempts: 3}\n"))
    assert "routing.max_atempts" in message
    assert "defaults.limits.rps" in failure(
        write(tmp_path, BASE + "defaults: {limits: {rps: 1}}\n")
    )


def test_fail_open_unknown_option_refused(tmp_path: Path) -> None:
    assert "fail_open.charge_zero" in failure(
        write(tmp_path, BASE + "fail_open: {charge_zero: true}\n")
    )


def test_float_money_refused(tmp_path: Path) -> None:
    text = BASE + (
        "bootstrap:\n  scopes: ['org:a']\n"
        "  budgets: [{id: b, scope: 'org:a', period: day, cap: 5.0}]\n"
    )
    message = failure(write(tmp_path, text))
    assert "bootstrap.budgets.0.cap" in message and "quoted amount" in message


def test_float_fx_rate_refused(tmp_path: Path) -> None:
    text = BASE + "fx: {rates: {EUR: 1.1}, as_of: '2026-11-13', source: s}\n"
    assert "fx.rates.EUR" in failure(write(tmp_path, text))


def test_budget_currency_must_match_ledger(tmp_path: Path) -> None:
    text = BASE + (
        "bootstrap:\n  scopes: ['org:a']\n"
        "  budgets: [{id: b, scope: 'org:a', period: day, cap: '5 EUR'}]\n"
    )
    assert "bootstrap.budgets.0.cap: must be in USD" in failure(write(tmp_path, text))


def test_base_url_may_be_an_env_reference(tmp_path: Path) -> None:
    text = BASE.replace('"https://api.openai.com/v1"', '"env:PROVIDER_BASE"')
    path = write(tmp_path, text)
    env = {**ENV, "PROVIDER_BASE": "https://res.example.com"}
    assert load(path, env=env).routes["m"].deployments[0].base_url == "https://res.example.com"
    assert "providers.openai.base_url" in failure(path, ENV)  # unset: names the variable
    bad = {**ENV, "PROVIDER_BASE": "http://res.example.com"}
    assert "must be an https://" in failure(path, bad)  # the resolved value is still checked


def test_missing_env_var_names_the_variable_not_a_value(tmp_path: Path) -> None:
    env = {k: v for k, v in ENV.items() if k != "OPENAI_API_KEY"}
    message = failure(write(tmp_path), env)
    assert "providers.openai.credential" in message and "OPENAI_API_KEY is not set" in message


def test_missing_secret_file_names_the_path(tmp_path: Path) -> None:
    text = BASE.replace("env:SPENDGATE_KEY_PEPPER", "file:/nonexistent/pepper")
    message = failure(write(tmp_path, text))
    assert "server.key_pepper" in message and "/nonexistent/pepper" in message


def test_a_secret_file_that_is_not_utf8_is_a_config_error_naming_only_the_path(
    tmp_path: Path,
) -> None:
    pepper = tmp_path / "pepper"
    pepper.write_bytes(b"\xff\xfefake-pepper")
    text = BASE.replace("env:SPENDGATE_KEY_PEPPER", f"file:{pepper}")
    message = failure(write(tmp_path, text))
    assert f"server.key_pepper: secret file {pepper} is not UTF-8 text" in message
    assert "fake-pepper" not in message and "0xff" not in message
    with pytest.raises(LookupError) as raised:
        resolve(f"file:{pepper}", {})
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


@pytest.mark.parametrize("pepper", ["tiny-fake-xyz", "fifteen-bytes-x", "\u00e9" * 7])
def test_a_key_pepper_under_16_bytes_is_a_config_error_that_never_shows_it(
    tmp_path: Path, pepper: str
) -> None:
    # Threat model C-01: the pepper keys the stored hashes; a short one must not start anything.
    path = write(tmp_path)
    message = failure(path, ENV | {"SPENDGATE_KEY_PEPPER": pepper})
    assert "server.key_pepper: must be at least 16 bytes" in message and pepper not in message
    report = check_config(path, env=ENV | {"SPENDGATE_KEY_PEPPER": pepper})
    assert not report.ok and any(e.startswith("server.key_pepper: ") for e in report.errors)
    assert all(pepper not in e for e in report.errors)
    file = tmp_path / "pepper"
    file.write_text(f"  {pepper}\n", encoding="utf-8")  # stripped like every secret
    message = failure(write(tmp_path, BASE.replace("env:SPENDGATE_KEY_PEPPER", f"file:{file}")))
    assert "server.key_pepper: must be at least 16 bytes" in message and pepper not in message


@pytest.mark.parametrize("pepper", ["sixteen-bytes-xx", "\u00e9" * 8])
def test_a_key_pepper_of_16_bytes_is_accepted(tmp_path: Path, pepper: str) -> None:
    settings = load(write(tmp_path), env=ENV | {"SPENDGATE_KEY_PEPPER": pepper})
    assert settings.key_pepper.reveal() == pepper


def test_literal_secret_in_yaml_is_refused_and_never_echoed(tmp_path: Path) -> None:
    text = BASE.replace("env:OPENAI_API_KEY", SECRET)
    message = failure(write(tmp_path, text))
    assert "providers.openai.credential" in message and SECRET not in message


def test_resolved_secret_is_not_in_reprs_or_errors(tmp_path: Path) -> None:
    s = load(write(tmp_path), env=ENV)
    assert SECRET not in repr(s) and SECRET not in repr(s.routes)
    assert SECRET not in str(s.routes["m"].deployments[0].credential)
    assert s.key_pepper.reveal() == "pepper-value-fake-0123"
    # A bad config in the same run must not echo the loaded values either.
    message = failure(write(tmp_path, BASE + "routing: {max_attempts: 0}\n"))
    assert SECRET not in message and "pepper-value-fake-0123" not in message


def test_unpriced_routed_model_is_an_error_and_listed(tmp_path: Path) -> None:
    path = write(tmp_path, BASE.replace("gpt-6-luna", "gpt-0-nothing"))
    message = failure(path)
    assert "models.m.catalog_id" in message
    assert "catalog.paths" in message and "spendgate catalog draft" in message  # how to price it
    report = check_config(path, env=ENV)
    assert not report.ok and report.unpriced == ("openai/gpt-0-nothing",)


def test_priced_at_a_given_time(tmp_path: Path) -> None:
    path = write(tmp_path)
    before_catalog = 1_000_000_000_000  # 2001
    assert "models.m.catalog_id" in failure_at(path, before_catalog)


def failure_at(path: Path, now_ms: int) -> str:
    with pytest.raises(ConfigError) as info:
        load(path, env=ENV, now_ms=now_ms)
    return str(info.value)


def test_deployment_provider_must_match_catalog_id(tmp_path: Path) -> None:
    text = BASE.replace(
        "[{provider: openai}]", "[{provider: anthropic, base_url: 'https://a.example'}]"
    )
    assert "models.m.deployments.0.provider" in failure(write(tmp_path, text))


def test_missing_fx_rate_refuses_start(tmp_path: Path) -> None:
    message = failure(write(tmp_path, BASE.replace("currency: USD", "currency: EUR")))
    assert message.startswith(str(tmp_path)) and "fx:" in message and "USD to EUR" in message


@pytest.mark.parametrize("value", ["-1", "'60s'", "0", "1.5"])
def test_bad_duration_names_the_path(tmp_path: Path, value: str) -> None:
    text = BASE.replace("backend:", f"lease_seconds: {value}, backend:")
    assert "ledger.lease_seconds" in failure(write(tmp_path, text))


def test_heartbeat_must_be_shorter_than_lease(tmp_path: Path) -> None:
    text = BASE.replace("backend:", "lease_seconds: 30, heartbeat_seconds: 30, backend:")
    assert "heartbeat_seconds must be shorter" in failure(write(tmp_path, text))


SQLITE_BACKEND = "backend: {kind: sqlite, path: ./l.db}"
POSTGRES_BACKEND = 'backend: {kind: postgres, dsn: "env:SPENDGATE_DATABASE_URL"}'


@pytest.mark.parametrize(
    ("ledger", "migrates"),
    [
        (SQLITE_BACKEND, True),  # one file on one host: nothing older can run on it
        (POSTGRES_BACKEND, False),  # shared: migrating is the operator's step (ADR-0018)
        ("auto_migrate: false, " + SQLITE_BACKEND, False),
        ("auto_migrate: true, " + POSTGRES_BACKEND, True),
    ],
)
def test_auto_migrate_defaults_by_backend_and_an_explicit_value_wins(
    tmp_path: Path, ledger: str, migrates: bool
) -> None:
    text = BASE.replace(SQLITE_BACKEND, ledger)
    assert load(write(tmp_path, text), env=ENV).config.ledger.migrates_on_start is migrates


def test_plain_http_only_for_loopback(tmp_path: Path) -> None:
    bad = write(tmp_path, BASE.replace("https://api.openai.com/v1", "http://api.openai.com/v1"))
    assert "models.m.deployments.0.base_url" in failure(bad)
    ok = write(tmp_path, BASE.replace("https://api.openai.com/v1", "http://127.0.0.1:9000/v1"))
    assert load(ok, env=ENV).routes["m"].deployments[0].base_url == "http://127.0.0.1:9000/v1"


@pytest.mark.parametrize(
    "url",
    [
        "http://x.test?x=@127.0.0.1/v1",
        "http://x.test#@localhost/v1",
        "http://local\thost/v1",
        "http://x.test\r\n@localhost/v1",
    ],
)
def test_plain_http_is_judged_by_the_host_the_client_parses(tmp_path: Path, url: str) -> None:
    # Written as YAML escapes inside the double-quoted scalar, so the loader sees the raw bytes.
    escaped = url.encode("unicode_escape").decode()
    bad = write(tmp_path, BASE.replace("https://api.openai.com/v1", escaped))
    assert "models.m.deployments.0.base_url: must be an https:// URL" in failure(bad)


def test_missing_credential_for_api_key_auth(tmp_path: Path) -> None:
    text = BASE.replace(', credential: "env:OPENAI_API_KEY"', "")
    assert "models.m.deployments.0" in failure(write(tmp_path, text))


def test_bootstrap_cross_checks(tmp_path: Path) -> None:
    text = BASE + ("bootstrap:\n  keys: [{name: k, scope: 'org:a', models: [nope]}]\n")
    message = failure(write(tmp_path, text))
    assert "bootstrap.keys.0.scope" in message and "bootstrap.keys.0.models.0" in message
    both = (
        BASE
        + "bootstrap:\n  scopes: ['org:a']\n"
        + "  keys: [{name: k, scope: 'org:a', admin_scopes: [usage:read]}]\n"
    )
    assert "bootstrap.keys.0" in failure(write(tmp_path, both))


def test_ledger_file_mode_checked(tmp_path: Path) -> None:
    db = tmp_path / "l.db"
    db.write_bytes(b"")
    db.chmod(0o644)
    assert "ledger.backend.path" in failure(write(tmp_path))
    db.chmod(0o600)
    assert load(write(tmp_path), env=ENV).ledger_path == db.resolve()
    assert os.stat(db).st_mode & 0o777 == 0o600


def test_unreadable_and_non_mapping_files(tmp_path: Path) -> None:
    assert str(tmp_path / "missing.yaml") in failure(tmp_path / "missing.yaml")
    assert "mapping" in failure(write(tmp_path, "- a\n- b\n"))
    assert "line" in failure(write(tmp_path, "server: [unclosed\n"))


def test_duplicate_yaml_key_refused(tmp_path: Path) -> None:
    message = failure(
        write(tmp_path, BASE + "routing: {max_attempts: 2}\nrouting: {max_attempts: 3}\n")
    )
    assert "duplicate key 'routing'" in message and "line" in message


def test_duplicate_key_in_catalog_override_refused(tmp_path: Path) -> None:
    (tmp_path / "o.yaml").write_text(
        "catalog_version: 1\ncatalog_version: 1\nmodels: []\n", encoding="utf-8"
    )
    message = failure(write(tmp_path, BASE + "catalog: {paths: [o.yaml]}\n"))
    assert "duplicate key" in message and "o.yaml" in message


def test_strict_loader_accepts_anchors_and_merge_keys() -> None:
    doc = yaml.load("a: &x {k: 1}\nb: {<<: *x, j: 2}\n", Loader=StrictLoader)  # noqa: S506
    assert doc == {"a": {"k": 1}, "b": {"k": 1, "j": 2}}


def test_catalog_override_invalid_refuses_start(tmp_path: Path) -> None:
    (tmp_path / "o.yaml").write_text("catalog_version: 1\nmodels: [{id: nope}]\n", encoding="utf-8")
    assert "catalog" in failure(write(tmp_path, BASE + "catalog: {paths: [o.yaml]}\n"))


HOOK_ENV = ENV | {
    "HOOK_URL": "https://hooks.example.test/budget?token=fake-hook-token",
    "HOOK_SECRET": "fake-signing-secret",  # pragma: allowlist secret
}
HOOK = (
    'alerts: {webhook: {url: "env:HOOK_URL", '
    'signing_secret: "env:HOOK_SECRET"}}\n'  # pragma: allowlist secret
)


def test_no_alert_webhook_by_default(tmp_path: Path) -> None:
    assert load(write(tmp_path), env=ENV).alert_webhook is None


def test_the_alert_webhook_resolves_its_url_and_secret_as_secrets(tmp_path: Path) -> None:
    hook = load(write(tmp_path, BASE + HOOK), env=HOOK_ENV).alert_webhook
    assert hook is not None
    assert hook.url.reveal() == HOOK_ENV["HOOK_URL"]
    assert hook.signing_secret.reveal() == "fake-signing-secret"  # pragma: allowlist secret
    assert (hook.timeout_seconds, hook.max_attempts, hook.queue_size) == (5, 5, 1000)
    assert "fake-hook-token" not in repr(hook) and "fake-signing-secret" not in repr(hook)


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.test/budget?token=fake-hook-token",
        "ftp://x.test/a",
        "https://",
        # An `@` after `?` or `#` is not userinfo: the host is x.test, not the loopback name.
        "http://x.test?x=@127.0.0.1/hook",
        "http://x.test#@localhost/",
        "http://local\thost/hook",  # a tab or a newline is refused, not stripped
        "http://x.test\r\n@localhost/hook",
    ],
)
def test_the_alert_webhook_url_must_be_https_and_is_never_echoed(tmp_path: Path, url: str) -> None:
    message = failure(write(tmp_path, BASE + HOOK), HOOK_ENV | {"HOOK_URL": url})
    assert "alerts.webhook.url: must be an https:// URL" in message
    assert "fake-hook-token" not in message and "x.test" not in message


def test_the_alert_webhook_may_use_plain_http_to_localhost(tmp_path: Path) -> None:
    env = HOOK_ENV | {"HOOK_URL": "http://127.0.0.1:9000/hook"}
    hook = load(write(tmp_path, BASE + HOOK), env=env).alert_webhook
    assert hook is not None and hook.url.reveal() == "http://127.0.0.1:9000/hook"


def test_the_alert_webhook_needs_references_not_values(tmp_path: Path) -> None:
    pasted = HOOK.replace('"env:HOOK_SECRET"', '"pasted-signing-value"')
    message = failure(write(tmp_path, BASE + pasted), HOOK_ENV)
    assert "alerts.webhook.signing_secret" in message and "pasted-signing-value" not in message
    unset = failure(write(tmp_path, BASE + HOOK), ENV)
    assert "alerts.webhook.url: environment variable HOOK_URL is not set" in unset
    assert "alerts.webhook.signing_secret: environment variable HOOK_SECRET" in unset


def test_the_alert_webhook_signing_secret_must_be_utf8_text(tmp_path: Path) -> None:
    # An undecodable environment byte, as Python escapes it.
    env = HOOK_ENV | {"HOOK_SECRET": "fake\udcff"}  # pragma: allowlist secret
    message = failure(write(tmp_path, BASE + HOOK), env)
    assert "alerts.webhook.signing_secret: must be UTF-8 text" in message
    assert "fake" not in message


@pytest.mark.parametrize("knob", ["max_attempts: 0", "max_attempts: 11", "queue_size: 0"])
def test_alert_webhook_bounds(tmp_path: Path, knob: str) -> None:
    text = BASE + HOOK.replace("signing_secret:", f"{knob}, signing_secret:")
    assert f"alerts.webhook.{knob.partition(':')[0]}" in failure(write(tmp_path, text), HOOK_ENV)
