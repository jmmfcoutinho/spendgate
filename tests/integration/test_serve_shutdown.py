"""`serve` hands keep-alive and the drain deadline to uvicorn, on every listener (ADR-0015)."""

from pathlib import Path

import pytest
import uvicorn

from spendgate.cli import serve
from spendgate.cli.runtime import open_runtime
from spendgate.config import ConfigError, Settings, load
from spendgate.config.schema import Server
from tests.integration.harness import CATALOG_OVERRIDE, CONFIG, ENV, MODELS, async_test


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
    """Run `serve` with no listener: every uvicorn config it builds, data port first."""
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
    data, admin = await _configs_built(tmp_path, monkeypatch, extra)
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
