"""`guard()` end to end: routes from the environment, SDK clients, currency, shared cap."""

import stat
import sys
from pathlib import Path

import httpx
import pytest

from spendgate.core.money import FxSnapshot
from spendgate.local import CapMismatch, Guard, GuardError, guard
from tests.fakes import FakeUpstream
from tests.integration.harness import CATALOG_OVERRIDE, ENV
from tests.integration.test_local_support import BODY, fake_client, now, settings_in

KEYS = {
    "openai": "sk-test-openai-not-real",
    "anthropic": "sk-ant-test-not-real",
}  # pragma: allowlist secret
CHAT = {"model": "gpt-6-luna", "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def make(
    tmp_path: Path, fake: FakeUpstream | None = None, cap: str = "5 USD", **kw: object
) -> Guard:
    return guard(
        cap, ledger_path=tmp_path / "l.db", provider_keys=KEYS,
        client=fake_client(fake or FakeUpstream()), now_ms=now, **kw,  # type: ignore[arg-type]
    )  # fmt: skip


def test_without_credentials_guard_names_the_variables(tmp_path: Path) -> None:
    with pytest.raises(GuardError, match="OPENAI_API_KEY"):
        guard("5 USD", ledger_path=tmp_path / "l.db")


def test_environment_credentials_are_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env-not-real")  # pragma: allowlist secret
    g = guard("5 USD", ledger_path=tmp_path / "l.db", client=fake_client(FakeUpstream()))
    with httpx.Client(transport=g.transport(), base_url="http://spendgate.local") as client:
        ids = {m["id"] for m in client.get("/v1/models").json()["data"]}
    g.close()
    assert "gpt-6-luna" in ids and "claude-haiku-5-5" not in ids  # only providers with a key


def test_extra_catalog_models_are_routed(tmp_path: Path) -> None:
    extra = tmp_path / "extra.yaml"
    extra.write_text(CATALOG_OVERRIDE)
    with (
        make(tmp_path, catalog=extra) as g,
        httpx.Client(transport=g.transport(), base_url="http://x") as client,
    ):
        ids = {m["id"] for m in client.get("/v1/models").json()["data"]}
    assert "whisper-test" in ids


def test_a_cap_in_another_currency_needs_an_fx_rate(tmp_path: Path) -> None:
    with pytest.raises(GuardError, match=r"no FX rate USD to EUR.*fx="):
        make(tmp_path, cap="5 EUR")


def test_a_cap_in_another_currency_with_fx_charges_in_that_currency(tmp_path: Path) -> None:
    fx = FxSnapshot.of("USD", "EUR", "0.5", "2026-11-17", "test rate")
    with (
        make(tmp_path, cap="5 EUR", fx=fx) as g,
        httpx.Client(transport=g.transport(), base_url="http://x") as client,
    ):
        reply = client.post("/v1/chat/completions", json=CHAT)
    assert reply.headers["x-spendgate-cost"] == "0.000001750 EUR"  # 3.5e-6 USD at 0.5


def test_the_cap_is_set_once_and_the_pool_is_shared(tmp_path: Path) -> None:
    fake = FakeUpstream()
    first = make(tmp_path, fake)
    again = make(tmp_path, fake)  # same cap: idempotent, same pool
    with httpx.Client(transport=first.transport(), base_url="http://x") as c1:
        spent = c1.post("/v1/chat/completions", json=CHAT).headers["x-spendgate-budget-remaining"]
    with httpx.Client(transport=again.transport(), base_url="http://x") as c2:
        left = c2.post("/v1/chat/completions", json=CHAT).headers["x-spendgate-budget-remaining"]
    assert left < spent  # the second guard sees what the first spent
    with pytest.raises(CapMismatch, match=r"already has a cap of 5\.000000000 USD"):
        make(tmp_path, cap="9 USD")
    first.close()
    again.close()


def test_a_cap_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(GuardError, match="greater than zero"):
        make(tmp_path, cap="0 USD")


def test_a_config_file_replaces_the_synthesized_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    settings_in(tmp_path)  # writes spendgate.yaml with the `alpha` alias and a USD ledger
    config = tmp_path / "spendgate.yaml"
    g = guard(
        "5 USD", config=config, ledger_path=tmp_path / "l.db",
        client=fake_client(FakeUpstream()), now_ms=now,
    )  # fmt: skip
    with httpx.Client(transport=g.transport(), base_url="http://x") as client:
        assert client.post("/v1/chat/completions", json=BODY).status_code == 200
    g.close()
    with pytest.raises(GuardError, match="ledger currency"):
        guard("5 EUR", config=config, ledger_path=tmp_path / "e.db")
    with pytest.raises(GuardError, match="config= already names"):
        guard("5 USD", config=config, provider_keys=KEYS)


@pytest.mark.skipif(sys.platform != "linux", reason="XDG layout")
def test_default_ledger_lives_in_the_user_data_dir_privately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    guard("5 USD", provider_keys=KEYS, client=fake_client(FakeUpstream()), now_ms=now).close()
    ledger = tmp_path / "data" / "spendgate" / "local-ledger.db"
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
    assert stat.S_IMODE(ledger.parent.stat().st_mode) == 0o700
