"""Tracing is off unless the `otel` extra is installed and enabled (issue #34, ADR-0019).

This file never imports OpenTelemetry, so it runs on a base install too. Without the extra,
`serve` with `observability.otel.enabled: true` warns and serves untraced instead of failing.
"""

import dataclasses
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from spendgate.cli import serve
from spendgate.config import Settings
from spendgate.pipeline import tracing
from tests.integration.harness import Stack, async_test, open_stack

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
CONFIG = """\
server: {listen: 127.0.0.1:8080, key_pepper: "env:SPENDGATE_KEY_PEPPER"}
ledger: {currency: USD, backend: {kind: sqlite, path: ./l.db}}
providers: {openai: {base_url: "https://api.openai.com/v1", credential: "env:OPENAI_API_KEY"}}
models:
  m: {catalog_id: openai/gpt-6-luna, deployments: [{provider: openai}]}
observability: {otel: {enabled: true}}
"""
# Runs in a fresh interpreter where importing argv[2] fails: the whole extra (a base install), or
# only the exporter (the SDK installed by something else).
WITHOUT_THE_EXTRA = """\
import asyncio, sys
sys.modules[sys.argv[2]] = None
from spendgate.cli import serve
from spendgate.config import load

async def main():
    env = {"SPENDGATE_KEY_PEPPER": "pepper-fake-0123456789"}
    env["OPENAI_API_KEY"] = "fake-provider-key"  # pragma: allowlist secret
    settings = load(sys.argv[1], env=env)
    assert settings.config.observability.otel.enabled
    async with serve.upstream(settings) as (sender, tracer):
        print(type(sender).__name__, tracer)

print(serve.otel_installed())
asyncio.run(main())
"""


def run(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and code, no user input
        [sys.executable, "-c", code, *args], capture_output=True, text=True, check=True
    )


@pytest.mark.parametrize("missing", ["opentelemetry", "opentelemetry.exporter"])
def test_enabled_without_the_extra_warns_and_serves_untraced(tmp_path: Path, missing: str) -> None:
    config = tmp_path / "spendgate.yaml"
    config.write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    out = run(WITHOUT_THE_EXTRA, str(config), missing)
    assert out.stdout.splitlines() == ["False", "AsyncClient None"]
    assert serve.OTEL_MISSING in out.stderr


def test_serving_never_imports_opentelemetry_unless_tracing_is_on() -> None:
    code = (
        "import sys\n"
        "import spendgate.cli.serve, spendgate.gateway.wiring, spendgate.pipeline.pipeline\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] == 'opentelemetry'))\n"
    )
    assert run(code).stdout.strip() == "[]"


@async_test
async def test_without_a_tracer_no_span_is_made_and_no_traceparent_is_sent(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        body = {"model": "alpha", "max_tokens": 100, "messages": [{"role": "user", "content": "x"}]}
        headers = {"traceparent": TRACEPARENT}
        assert (await s.post("/v1/chat/completions", body, headers=headers)).is_success
        assert "traceparent" not in s.fake.requests[0].headers


def test_the_no_tracer_is_a_no_op() -> None:
    quiet = tracing.NoTracer()
    assert quiet.parent({"traceparent": TRACEPARENT}) is None
    with quiet.span(tracing.HOLD, None, {"spendgate.model": "m"}) as span:
        span.set("spendgate.outcome", "held")


def otel_settings(stack: Stack, *, enabled: bool, trust: bool = False) -> Settings:
    """The stack's settings with `observability.otel.enabled` and `trust_inbound_context` set."""
    config = stack.settings.config
    fields = {"enabled": enabled, "trust_inbound_context": trust}
    observability = config.observability.model_copy(
        update={"otel": config.observability.otel.model_copy(update=fields)}
    )
    new = config.model_copy(update={"observability": observability})
    return dataclasses.replace(stack.settings, config=new)
