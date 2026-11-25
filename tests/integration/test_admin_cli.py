"""The CLI end to end: create a key, spend through the gateway against the fake, read it back."""

import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path

import httpx
import pytest

from spendgate import package_version
from spendgate.cli import main
from spendgate.cli.client import Client, CliError
from spendgate.cli.commands import COMMANDS
from spendgate.gateway.admin import create_admin_app
from spendgate.ledger.key_types import AdminScope
from tests.integration.harness import ENV, NOW_MS, PEPPER, async_test
from tests.integration.test_admin_support import open_admin

SCOPE = "org:acme/user:u1"


async def cli(capsys: pytest.CaptureFixture[str], config: Path, *argv: str) -> tuple[int, str, str]:
    code = await asyncio.to_thread(main, [*argv, "--config", str(config)])
    out = capsys.readouterr()
    return code, out.out, out.err


@async_test
async def test_create_a_key_spend_and_see_it_in_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SPENDGATE_ADMIN_KEY", raising=False)
    async with open_admin(tmp_path) as a:
        config = tmp_path / "spendgate.yaml"
        code, out, _ = await cli(
            capsys, config, "keys", "create", "--local", "--json", "--scope", SCOPE,
            "--name", "cli-made", "--models", "alpha",
        )  # fmt: skip
        assert code == 0
        secret = json.loads(out)["secret"]
        assert secret.startswith("sg-")
        body: dict[str, object] = {"model": "alpha", "max_tokens": 5}
        body["messages"] = [{"role": "user", "content": "hi"}]
        sent = await a.stack.post("/v1/chat/completions", body, key=secret)
        assert sent.status_code == 200, sent.text
        charged = sent.headers["x-spendgate-cost"].split(" ")[0]
        # An absolute time: a duration such as 7d is counted back from the real clock, which
        # moves past the harness's fixed call time and would leave the call outside the window.
        code, out, _ = await cli(
            capsys, config, "usage", "--local", "--by", "service,model",
            "--since", "2026-11-16T00:00:00Z",
        )  # fmt: skip
        assert code == 0
        assert "openai/gpt-6-luna" in out and charged in out and "CALLS" in out
        code, out, _ = await cli(capsys, config, "usage", "--local", "--json", "--by", "model")
        assert json.loads(out)["rows"][0]["cost"]["amount"] == charged
        # The SCOPE column is the path a person typed, not the scope's id.
        code, out, _ = await cli(capsys, config, "keys", "list", "--local")
        scope_id = (await a.ok("GET", f"/admin/v1/scopes/{SCOPE}"))["id"]
        assert code == 0 and SCOPE in out and str(scope_id) not in out
        # The write went through the same ledger function, so it is audited under actor `cli`.
        audit = await a.ok("GET", "/admin/v1/audit", params={"actor": "cli"})
        assert [e["action"] for e in audit["data"]] == ["key.create"]  # type: ignore[attr-defined]


@async_test
async def test_local_commands_budgets_kill_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    async with open_admin(tmp_path) as a:
        config = tmp_path / "spendgate.yaml"
        code, out, _ = await cli(
            capsys, config, "budgets", "set", "--local", "org:acme", "--cap", "20 USD",
            "--period", "month",
        )  # fmt: skip
        assert code == 0 and "20.000000000 USD" in out
        code, out, _ = await cli(
            capsys, config, "budgets", "set", "--local", "org:acme", "--cap", "25 USD",
            "--period", "month", "--json",
        )  # fmt: skip
        budget = json.loads(out)
        assert budget["cap"]["amount"] == "25.000000000"  # same period: updated, not duplicated
        code, out, _ = await cli(capsys, config, "budgets", "list", "--local", "org:acme")
        assert out.count("month") == 1
        code, out, _ = await cli(
            capsys, config, "budgets", "grant", "--local", budget["id"], "--amount", "5 USD",
            "--until", "30d", "--reason", "launch",
        )  # fmt: skip
        assert code == 0 and "5.000000000 USD" in out
        code, out, _ = await cli(capsys, config, "kill", "on", "--local", "--reason", "drill")
        switch = out.split()[2]
        # The hint repeats --local and --config, so pasting it lifts this switch.
        assert out.strip().endswith(f"spendgate kill off {switch} --local --config {config}")
        assert (await a.stack.chat()).status_code == 503
        code, out, _ = await cli(capsys, config, "status", "--local")
        assert code == 0 and switch in out and "kill switches: 1" in out
        assert f"spendgate {importlib.metadata.version('spendgate')}," in out  # not 0.0.0
        assert (await cli(capsys, config, "kill", "off", "--local", switch))[0] == 0
        assert (await a.stack.chat()).status_code == 200


@async_test
async def test_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SPENDGATE_ADMIN_KEY", raising=False)
    async with open_admin(tmp_path) as a:
        config = tmp_path / "spendgate.yaml"
        code, _, err = await cli(capsys, config, "keys", "revoke", "--local", "nope")
        assert code == 1 and "not_found" in err
        code, _, err = await cli(capsys, config, "status")  # no admin key in the environment
        assert code == 4 and "SPENDGATE_ADMIN_KEY" in err

        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(refuse), base_url="http://a"
        ) as down:
            with pytest.raises(CliError, match="cannot reach") as caught:
                await COMMANDS["status"](Client(down), None)  # type: ignore[arg-type]
            assert caught.value.code == 5
        code, _, err = await cli(capsys, tmp_path / "missing.yaml", "check-config")
        assert code == 3
        code, out, _ = await cli(capsys, config, "check-config")
        assert code == 0 and "configuration ok" in out
        _ = a


@async_test
async def test_http_transport_authenticates_with_the_admin_key_and_maps_refusals(
    tmp_path: Path,
) -> None:
    async with open_admin(tmp_path) as a:
        app = create_admin_app(a.ctx, shared_port=True)
        headers = {"Authorization": f"Bearer {a.secret}"}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://a", headers=headers
        ) as h:
            client = Client(h)
            data, text = await COMMANDS["status"](client, None)  # type: ignore[arg-type]
            version = package_version()
            assert data["version"] == version and f"spendgate {version}" in text
        async with httpx.AsyncClient(transport=transport, base_url="http://a") as anon:
            with pytest.raises(CliError, match="unauthorized") as caught:
                await COMMANDS["status"](Client(anon), None)  # type: ignore[arg-type]
            assert caught.value.code == 4
        assert os.environ.get("SPENDGATE_ADMIN_KEY") != a.secret


@async_test
async def test_keys_create_prints_the_secret_when_the_admin_key_cannot_read_scopes(
    tmp_path: Path,
) -> None:
    async with open_admin(tmp_path) as a:
        writer = await a.stack.ledger.create_admin_key(
            a.stack.scope.id, "writer", frozenset({AdminScope.KEYS_WRITE}),
            pepper=PEPPER, actor="boot", now=NOW_MS,
        )  # fmt: skip
        headers = {"Authorization": f"Bearer {writer.secret}"}
        transport = httpx.ASGITransport(app=create_admin_app(a.ctx, shared_port=True))
        args = argparse.Namespace(
            scope=SCOPE, name="w", expires=None, models=None, rpm=None, tpm=None,
            max_concurrent=None, max_output_tokens=None,
        )  # fmt: skip
        async with httpx.AsyncClient(
            transport=transport, base_url="http://a", headers=headers
        ) as h:
            made, text = await COMMANDS["keys create"](Client(h), args)
        # No usage:read: the SCOPE column falls back to the id, and the secret still prints.
        assert str(made["secret"]) in text and str(made["scope_id"]) in text
