"""`serve` hands keep-alive and the drain deadline to uvicorn, on every listener (ADR-0015)."""

import asyncio
import signal
from pathlib import Path

import httpx
import pytest
import uvicorn

from spendgate.cli import serve
from spendgate.cli.runtime import open_runtime
from spendgate.config import ConfigError, Settings, load
from spendgate.config.schema import Server
from spendgate.core.outcomes import Outcome, ReservationState
from tests.fakes import Script
from tests.integration.harness import (
    CATALOG_OVERRIDE,
    CHAT,
    CONFIG,
    ENV,
    MODELS,
    async_test,
    open_stack,
    wait_until,
)


def _settings(tmp_path: Path, server_extra: str) -> Settings:
    # The harness config ends the `server` mapping after `max_concurrent_requests`.
    text = CONFIG.format(
        max_body=1_000_000, max_json_body=1_000_000, concurrent=f"8, {server_extra}",
        attempts=2, cooldown=5, models=MODELS,
    )  # fmt: skip
    (tmp_path / "spendgate.yaml").write_text(text)
    (tmp_path / "catalog_override.yaml").write_text(CATALOG_OVERRIDE)
    return load(tmp_path / "spendgate.yaml", env=ENV)


async def _configs_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_extra: str
) -> list[uvicorn.Config]:
    """Run `serve` with no listener: every uvicorn config it builds, the admin one first."""
    built: list[uvicorn.Config] = []
    real = serve.uvicorn_config

    def spy(*args: object, **kw: object) -> uvicorn.Config:
        config = real(*args, **kw)  # type: ignore[arg-type]
        built.append(config)
        return config

    async def no_listener(_self: uvicorn.Server, sockets: object = None) -> None:
        return None

    monkeypatch.setattr(serve, "uvicorn_config", spy)
    monkeypatch.setattr(uvicorn.Server, "serve", no_listener)  # the admin follower inherits it
    async with open_runtime(_settings(tmp_path, server_extra)) as rt:
        await serve._serve(rt, bootstrap=False)
    return built


@async_test
async def test_server_keep_alive_and_graceful_shutdown_reach_both_uvicorn_listeners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = (
        'keep_alive_seconds: 91, graceful_shutdown_seconds: 17, admin: {listen: "127.0.0.1:9090"}'
    )
    admin, data = await _configs_built(tmp_path, monkeypatch, extra)
    assert (data.port, admin.port) == (8080, 9090)
    for config in (data, admin):
        assert (config.timeout_keep_alive, config.timeout_graceful_shutdown) == (91, 17)
    assert (data.lifespan, admin.lifespan) == ("on", "off")


@async_test
async def test_the_shared_port_gets_the_default_keep_alive_and_drain_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (only,) = await _configs_built(tmp_path, monkeypatch, "max_request_seconds: 900")
    defaults = Server.model_fields
    assert only.timeout_keep_alive == defaults["keep_alive_seconds"].default == 75
    assert only.timeout_graceful_shutdown == defaults["graceful_shutdown_seconds"].default == 30


def test_keep_alive_and_graceful_shutdown_must_be_positive(tmp_path: Path) -> None:
    for bad in ("keep_alive_seconds: 0", "graceful_shutdown_seconds: -1"):
        with pytest.raises(ConfigError):
            _settings(tmp_path, bad)


def test_a_signal_to_the_data_server_starts_the_admin_drain_at_the_same_time() -> None:
    """The admin listener drains alongside the data one, so a stop takes one deadline, not two."""
    server = Server.model_validate({"listen": "127.0.0.1:8080", "key_pepper": "env:TEST_PEPPER"})
    at = serve.Listen("127.0.0.1", 0)
    admin = serve._Follower(serve.uvicorn_config(object(), at, server, lifespan="off"))
    data = serve._Leader(serve.uvicorn_config(object(), at, server, lifespan="on"), admin)
    data.handle_exit(signal.SIGTERM, None)
    assert (data.should_exit, admin.should_exit, admin.force_exit) == (True, True, False)
    data.handle_exit(signal.SIGINT, None)  # a SIGINT during the drain forces both out
    assert (data.force_exit, admin.force_exit) == (True, True)


# M-13
@async_test
async def test_m13_a_call_still_running_at_the_drain_deadline_is_settled_at_the_full_hold(
    tmp_path: Path,
) -> None:
    """Real uvicorn on a unix socket (the test socket guard allows it), shortest deadline (1 s)."""
    async with open_stack(tmp_path) as s:
        server = s.settings.config.server.model_copy(update={"graceful_shutdown_seconds": 1})
        config = serve.uvicorn_config(s.app, serve.Listen("127.0.0.1", 0), server, lifespan="on")
        config.uds = str(tmp_path / "gw.sock")
        gateway = uvicorn.Server(config)
        serving = asyncio.ensure_future(gateway.serve())
        await wait_until(lambda: gateway.started)
        transport = httpx.AsyncHTTPTransport(uds=config.uds)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw.test") as client:
            s.fake.enqueue(Script(hang=True))
            messages = [{"role": "user", "content": "hi"}]
            body = {"model": "alpha", "max_tokens": 100, "messages": messages}
            auth = {"Authorization": f"Bearer {s.secret}"}
            call = asyncio.ensure_future(client.post(CHAT, json=body, headers=auth))
            await wait_until(lambda: s.fake.calls >= 1)  # held and on the wire
            gateway.should_exit = True  # what SIGTERM does
            await serving
            answered = await call
        assert answered.status_code == 500  # uvicorn's own plain answer to the stopped call
        held = s.observer.held[0]
        assert await s.totals() == (held.hold.nano, 0)
        calls = await s.ledger.get_calls(held.request_id)
        assert [(c.outcome, c.charged_nano) for c in calls] == [(Outcome.CANCELLED, held.hold.nano)]
        reservation = await s.ledger.get_reservation(held.request_id)
        assert reservation is not None and reservation.state is ReservationState.SETTLED
