"""Cloud identity settings: `credential: adc`, `ledger.backend.auth`, pool sizes, file re-reads."""

import asyncio
from pathlib import Path

import pytest

from spendgate.cli.client import EXIT_CONFIG, CliError
from spendgate.cli.runtime import _postgres
from spendgate.config import Secret, load, reread_file_secrets
from spendgate.config.secrets import CREDENTIAL_HELP, check_credential_ref
from tests.unit.test_config import BASE, ENV, failure, write

GOOGLE = """\
server: {listen: 127.0.0.1:8080, key_pepper: "env:SPENDGATE_KEY_PEPPER"}
ledger: {currency: USD, backend: {kind: sqlite, path: ./l.db}}
providers:
  google: {base_url: "https://vertex.example.test", auth: bearer_token, credential: adc,
           region: europe-west4}
models:
  g: {catalog_id: google/gemini-4-carbon, deployments: [{provider: google}]}
"""
POSTGRES = 'ledger: {{currency: USD, backend: {{kind: postgres, dsn: "{dsn}"{auth}}}{pool}}}\n'


def _with_ledger(dsn: str = "env:SPENDGATE_DATABASE_URL", auth: str = "", pool: str = "") -> str:
    lines = BASE.splitlines(keepends=True)
    return lines[0] + POSTGRES.format(dsn=dsn, auth=auth, pool=pool) + "".join(lines[2:])


def test_adc_is_accepted_for_vertex_and_resolves_to_no_stored_secret(tmp_path: Path) -> None:
    settings = load(write(tmp_path, GOOGLE), env=ENV)
    built = settings.routes["g"].deployments[0]
    assert built.ambient is True and built.credential is None


@pytest.mark.parametrize(
    ("deployment", "path"),
    [
        ("provider: openai, auth: bearer_token, credential: adc", "models.m.deployments.0"),
        ("provider: openai, credential: adc", "models.m.deployments.0"),  # api_key auth
    ],
)
def test_adc_is_refused_outside_google_vertex(tmp_path: Path, deployment: str, path: str) -> None:
    text = BASE.replace("{provider: openai}", "{" + deployment + "}")
    message = failure(write(tmp_path, text))
    assert f"{path}.credential: adc is for Google Vertex AI only" in message


def test_a_bad_credential_reference_is_refused_without_echoing_it() -> None:
    with pytest.raises(ValueError, match="adc") as info:
        check_credential_ref("sk-pasted-real-key-0000")  # pragma: allowlist secret
    assert "sk-pasted" not in str(info.value) and str(info.value) == CREDENTIAL_HELP
    assert check_credential_ref("file:/run/secrets/x") == "file:/run/secrets/x"


def test_pool_settings_default_to_the_previous_constants_and_are_validated(
    tmp_path: Path,
) -> None:
    cfg = load(write(tmp_path, _with_ledger()), env=ENV).config.ledger
    assert (cfg.pool_min, cfg.pool_max, cfg.pool_timeout_seconds) == (1, 10, 10)
    sized = _with_ledger(pool=", pool_min: 0, pool_max: 3, pool_timeout_seconds: 4")
    cfg = load(write(tmp_path, sized), env=ENV).config.ledger
    assert (cfg.pool_min, cfg.pool_max, cfg.pool_timeout_seconds) == (0, 3, 4)
    message = failure(write(tmp_path, _with_ledger(pool=", pool_min: 5, pool_max: 2")))
    assert "pool_min must not exceed pool_max" in message


@pytest.mark.parametrize(
    ("auth", "kind"),
    [
        (", auth: {kind: rds_iam, region: eu-west-1}", "rds_iam"),
        (", auth: {kind: entra}", "entra"),
        (", auth: {kind: cloudsql_iam}", "cloudsql_iam"),
    ],
)
def test_ledger_token_auth_kinds_load(tmp_path: Path, auth: str, kind: str) -> None:
    backend = load(write(tmp_path, _with_ledger(auth=auth)), env=ENV).config.ledger.backend
    assert backend.kind == "postgres" and backend.auth is not None and backend.auth.kind == kind


def test_an_unknown_ledger_auth_kind_or_key_is_refused(tmp_path: Path) -> None:
    assert "ledger.backend" in failure(write(tmp_path, _with_ledger(auth=", auth: {kind: x}")))
    typo = _with_ledger(auth=", auth: {kind: entra, scop: y}")
    assert "ledger.backend.postgres.auth.entra.scop" in failure(write(tmp_path, typo))


class _Opened:
    """Stands in for `PostgresLedger.open`: records what the runtime passed, opens nothing."""

    def __init__(self) -> None:
        self.dsn = ""
        self.kwargs: dict[str, object] = {}

    async def __call__(self, dsn: str, **kwargs: object) -> "_Opened":
        self.dsn, self.kwargs = dsn, kwargs
        return self


def test_the_runtime_passes_pool_settings_and_a_token_source_to_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spendgate.ledger.postgres import PostgresLedger  # noqa: PLC0415 - optional extra

    opened = _Opened()
    monkeypatch.setattr(PostgresLedger, "open", opened)
    auth = ", auth: {kind: entra, scope: https://example.test/.default}"
    text = _with_ledger(auth=auth, pool=", pool_min: 2, pool_max: 4, pool_timeout_seconds: 3")
    settings = load(write(tmp_path, text), env=ENV)
    dsn = "postgresql://spendgate@db.example.test/spend"
    asyncio.run(_postgres(settings, dsn, migrate=False))
    assert opened.dsn == dsn
    kw = opened.kwargs
    assert (kw["min_size"], kw["max_size"], kw["pool_timeout"]) == (2, 4, 3.0)
    assert repr(kw["login"]) == "DbTokens(entra)" and kw["currency"] == "USD"
    asyncio.run(_postgres(load(write(tmp_path, _with_ledger()), env=ENV), dsn, migrate=False))
    assert opened.kwargs["login"] is None and opened.kwargs["max_size"] == 10


def test_a_dsn_with_a_password_under_token_auth_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spendgate.ledger.postgres import PostgresLedger  # noqa: PLC0415 - optional extra

    monkeypatch.setattr(PostgresLedger, "open", _Opened())
    settings = load(write(tmp_path, _with_ledger(auth=", auth: {kind: cloudsql_iam}")), env=ENV)
    secret_dsn = ENV["SPENDGATE_DATABASE_URL"]
    with pytest.raises(CliError) as info:
        asyncio.run(_postgres(settings, secret_dsn, migrate=False))
    assert info.value.code == EXIT_CONFIG and "must not carry a password" in str(info.value)
    assert "pw-secret" not in str(info.value)


# --- SIGHUP: `file:` secrets read again --------------------------------------------------------


def _file_config(tmp_path: Path) -> tuple[Path, Path, Path]:
    key, dsn = tmp_path / "openai_key", tmp_path / "dsn"
    key.write_text("sk-test-old-0000\n")  # pragma: allowlist secret
    dsn.write_text("postgresql://u:old-pw@db/spend\n")  # pragma: allowlist secret
    text = _with_ledger(dsn=f"file:{dsn}").replace("env:OPENAI_API_KEY", f"file:{key}")
    return write(tmp_path, text), key, dsn


def test_file_secrets_are_read_again_and_reported_by_path_only(tmp_path: Path) -> None:
    path, key, dsn = _file_config(tmp_path)
    settings = load(path, env=ENV)
    key.write_text("sk-test-new-0000")  # pragma: allowlist secret
    result = reread_file_secrets(settings)
    assert result.reloaded == ("providers.openai.credential", "ledger.backend.dsn")
    assert result.changed == ("providers.openai.credential",) and result.failed == ()
    assert result.settings.routes["m"].deployments[0].credential == Secret("sk-test-new-0000")
    assert result.settings.ledger_dsn == settings.ledger_dsn
    assert result.settings.config is settings.config  # nothing but secret values changes
    dsn.write_text("postgresql://u:new-pw@db/spend")  # pragma: allowlist secret
    assert reread_file_secrets(result.settings).changed == ("ledger.backend.dsn",)


def test_an_unreadable_secret_keeps_its_old_value_and_is_reported(tmp_path: Path) -> None:
    path, key, _ = _file_config(tmp_path)
    settings = load(path, env=ENV)
    key.unlink()
    result = reread_file_secrets(settings)
    assert result.failed[0][0] == "providers.openai.credential"
    assert "cannot be read" in result.failed[0][1] and "sk-test" not in result.failed[0][1]
    assert result.settings.routes["m"].deployments[0].credential == Secret("sk-test-old-0000")
    assert result.reloaded == ("ledger.backend.dsn",)


def test_env_secrets_are_not_read_again(tmp_path: Path) -> None:
    settings = load(write(tmp_path), env=ENV)
    result = reread_file_secrets(settings)
    assert result.reloaded == () and result.changed == () and result.settings == settings
