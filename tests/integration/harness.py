"""Gateway test rig: Starlette app, pipeline, SQLite ledger and the fake upstream, no sockets.

`open_stack` builds the whole proxy in-process with `build_gateway`, the composition root that
`spendgate serve` uses: requests enter through `httpx.ASGITransport`, the pipeline talks to
`FakeUpstream` through its transport, and money lives in a real SQLite ledger. Two OpenAI
deployments (`a.` and `b.` hosts) serve the alias `alpha`, so failover is observable in the
fake's request log (the `host` header).
"""

import asyncio
import functools
import textwrap
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
from starlette.applications import Starlette

from spendgate.config import Settings, load
from spendgate.core.budgets import Budget, Period, ScopeKind
from spendgate.core.money import Money
from spendgate.core.sessions import SessionDefaults
from spendgate.gateway.compose import Gateway, build_gateway
from spendgate.gateway.metrics import Metrics
from spendgate.ledger.key_types import KeySpec
from spendgate.ledger.keys import KeyService
from spendgate.ledger.port import MoneyLedger, ScopeRow
from spendgate.ledger.sqlite import SqliteLedger
from spendgate.pipeline.adapters import build_providers
from spendgate.pipeline.faults import InternalEvent
from spendgate.pipeline.model import (
    AttemptEvent,
    HeldEvent,
    LedgerEvent,
    RefusedEvent,
    SettledEvent,
)
from spendgate.pipeline.pipeline import Pipeline
from spendgate.providers.bedrock import AwsCredentials, Bedrock
from spendgate.providers.google import GoogleProvider
from spendgate.providers.port import Provider
from tests.conftest import FakeClock
from tests.fakes import FakeUpstream

NOW_MS = int(datetime(2026, 11, 17, tzinfo=UTC).timestamp()) * 1000  # catalog entries in force
PEPPER = b"test-pepper-0123456789abcdef"
ENV = {
    "TEST_PEPPER": PEPPER.decode(),
    "OPENAI_KEY": "sk-test-openai-not-real",  # pragma: allowlist secret
    "ANTHROPIC_KEY": "sk-ant-test-not-real",  # pragma: allowlist secret
    "GOOGLE_KEY": "fake-gemini-key-0000",  # pragma: allowlist secret
}
# AWS's documented example credentials, not real.
AWS_EXAMPLE = AwsCredentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
CHAT = "/v1/chat/completions"
MESSAGES_PATH = "/v1/messages"

MODELS = """\
models:
  alpha:
    catalog_id: openai/gpt-6-luna
    deployments:
      - {provider: openai, base_url: "https://a.upstream.test"}
      - {provider: openai, base_url: "https://b.upstream.test"}
  alpha-one:
    catalog_id: openai/gpt-6-luna
    deployments: [{provider: openai, base_url: "https://a.upstream.test"}]
  embed:
    catalog_id: openai/text-embedding-3-small
    deployments: [{provider: openai, base_url: "https://a.upstream.test"}]
  haiku:
    catalog_id: anthropic/claude-haiku-5-5
    deployments: [{provider: anthropic, base_url: "https://anthropic.upstream.test"}]
  bedrock-haiku:
    catalog_id: bedrock/anthropic.claude-haiku-5-5
    deployments:
      - provider: bedrock
        base_url: "https://bedrock.upstream.test"
        auth: sigv4
        region: eu-west-1
  gemini:
    catalog_id: google/gemini-4-carbon
    deployments: [{provider: google, base_url: "https://google.upstream.test"}]
  whisper:
    catalog_id: openai/whisper-test
    deployments: [{provider: openai, base_url: "https://a.upstream.test"}]
  tts:
    catalog_id: openai/tts-test
    deployments: [{provider: openai, base_url: "https://a.upstream.test"}]
  alpha3:
    catalog_id: openai/gpt-6-luna
    deployments:
      - {provider: openai, base_url: "https://a.upstream.test"}
      - {provider: openai, base_url: "https://b.upstream.test"}
      - {provider: openai, base_url: "https://c.upstream.test"}
"""

# The bundled catalog has no OpenAI transcription or speech entry; these two are test-only.
CATALOG_OVERRIDE = """\
catalog_version: 1
models:
  - id: openai/whisper-test
    mode: transcription
    currency: USD
    effective_from: "2026-11-01"
    source_url: https://example.test/pricing
    verified: true
    meters: {audio_input_per_minute: "0.006"}
  - id: openai/tts-test
    mode: speech
    currency: USD
    effective_from: "2026-11-01"
    source_url: https://example.test/pricing
    verified: true
    meters: {per_1k_characters: "0.015"}
"""

CONFIG = """\
server: {{listen: "127.0.0.1:8080", key_pepper: "env:TEST_PEPPER", max_body_bytes: {max_body},
  max_json_body_bytes: {max_json_body},
  max_concurrent_requests: {concurrent}}}
ledger: {{currency: USD, backend: {{kind: sqlite, path: ./ledger.db}}}}
providers:
  openai: {{base_url: "https://openai.upstream.test", credential: "env:OPENAI_KEY"}}
  anthropic: {{base_url: "https://anthropic.upstream.test", credential: "env:ANTHROPIC_KEY"}}
  google: {{credential: "env:GOOGLE_KEY"}}
  bedrock: {{auth: sigv4}}
catalog: {{paths: [./catalog_override.yaml]}}
routing: {{max_attempts: {attempts}, cooldown_seconds: {cooldown}}}
{models}"""


def async_test[**P](fn: Callable[P, Awaitable[None]]) -> Callable[P, None]:
    """Run an async test body on a fresh loop (the suite has no async pytest plugin)."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(fn(*args, **kwargs))  # type: ignore[arg-type]

    return wrapper


async def wait_until(predicate: Callable[[], bool], *, seconds: float = 5.0) -> None:
    """Let ledger worker threads finish: polls the loop with tiny real sleeps, bounded.

    Not a timing assumption: the thing waited for is a state change (a request reached the fake,
    a settlement landed), and the deadline only turns a hang into a failure.
    """
    for _ in range(int(seconds / 0.002)):
        if predicate():
            return
        await asyncio.sleep(0.002)
    raise AssertionError("condition not reached")


class RaisingTransport(httpx.AsyncBaseTransport):
    """Every request fails with the exception `make` builds: a network that misbehaves."""

    def __init__(self, make: Callable[[httpx.Request], Exception]) -> None:
        self._make = make

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise self._make(request)


def usd(amount: str) -> int:
    return Money.from_amount(amount, "USD").nano


class Recorder:
    """An `Observer` that keeps every event, in order."""

    def __init__(self) -> None:
        self.refused: list[RefusedEvent] = []
        self.held: list[HeldEvent] = []
        self.attempts: list[AttemptEvent] = []
        self.settled: list[SettledEvent] = []
        self.internal: list[InternalEvent] = []
        self.ledger: list[LedgerEvent] = []
        self.heartbeat_failures: list[str] = []

    def on_refused(self, event: RefusedEvent) -> None:
        self.refused.append(event)

    def on_held(self, event: HeldEvent) -> None:
        self.held.append(event)

    def on_attempt(self, event: AttemptEvent) -> None:
        self.attempts.append(event)

    def on_settled(self, event: SettledEvent) -> None:
        self.settled.append(event)

    def on_internal(self, event: InternalEvent) -> None:
        self.internal.append(event)

    def on_ledger(self, event: LedgerEvent) -> None:
        self.ledger.append(event)

    def on_heartbeat_failed(self, step: str) -> None:
        self.heartbeat_failures.append(step)


@dataclass(slots=True)
class Stack:
    settings: Settings
    pipeline: Pipeline
    app: Starlette
    client: httpx.AsyncClient
    fake: FakeUpstream
    clock: FakeClock
    ledger: SqliteLedger
    keys: KeyService
    secret: str
    scope: ScopeRow
    budget: Budget
    observer: Recorder
    gateway: Gateway  # what `serve` would run: both apps, upkeep, admin context
    extra_keys: dict[str, str] = field(default_factory=dict)

    async def post(
        self, path: str, body: dict[str, object], *, key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:  # fmt: skip
        sent = {"Authorization": f"Bearer {key or self.secret}"} | (headers or {})
        return await self.client.post(path, json=body, headers=sent)

    async def chat(self, model: str = "alpha", **extra: object) -> httpx.Response:
        body: dict[str, object] = {"model": model, "max_tokens": 100}
        body["messages"] = [{"role": "user", "content": "hi"}]
        return await self.post(CHAT, body | extra)

    async def totals(self) -> tuple[int, int]:
        """(spent, held) nano on the key's budget."""
        rows = await self.ledger.totals(self.scope.id, NOW_MS)
        row = next(r for r in rows if r.budget_id == self.budget.id)
        return row.spent_nano, row.held_nano

    def assert_no_contact(self) -> None:
        self.fake.assert_no_contact()


@asynccontextmanager
async def open_stack(  # noqa: PLR0913 - a rig with named knobs
    tmp_path: Path, *, fake: FakeUpstream | None = None, budget: str = "10", attempts: int = 2,
    cooldown: int = 5,
    models: str = MODELS, max_body: int = 26_214_400, concurrent: int = 128,
    session_defaults: SessionDefaults | None = None, rpm: int | None = None,
    tpm: int | None = None, max_concurrent: int | None = None,
    allowed_models: tuple[str, ...] | None = None, per_call: int | None = None,
    pipeline_kwargs: dict[str, object] | None = None,
    wrap_ledger: Callable[[SqliteLedger], MoneyLedger] | None = None,
    providers: Mapping[str, Provider] | None = None, metrics: Metrics | None = None,
    admin_listen: str | None = None, catalog_override: str = "",
) -> AsyncIterator[Stack]:  # fmt: skip
    fake = fake or FakeUpstream()
    clock = FakeClock(NOW_MS)
    path = tmp_path / "spendgate.yaml"
    text = CONFIG.format(
        max_body=max_body,
        max_json_body=min(max_body, 4_194_304),  # JSON has its own cap (SR-01)
        concurrent=concurrent,
        attempts=attempts,
        models=models,
        cooldown=cooldown,
    )
    if admin_listen is not None:  # a separate admin listener (`server.admin`)
        main = 'server: {listen: "127.0.0.1:8080",'
        text = text.replace(main, f'{main} admin: {{listen: "{admin_listen}"}},', 1)
    path.write_text(text)
    # Extra test entries go after the shared ones: `catalog_override` is a YAML list of models.
    (tmp_path / "catalog_override.yaml").write_text(CATALOG_OVERRIDE + catalog_override)
    settings = load(path, env=ENV)
    assert settings.ledger_path is not None
    ledger = SqliteLedger.open(str(settings.ledger_path), currency="USD")
    sender = fake.client()
    keys = KeyService(ledger, PEPPER)
    org = await ledger.create_scope(ScopeKind.ORG, "acme", None, now=NOW_MS)
    scope = await ledger.create_scope(ScopeKind.USER, "u1", org.id, now=NOW_MS)
    cap = await ledger.create_budget(scope.id, Period.NONE, usd(budget), now=NOW_MS)
    spec = KeySpec(
        scope.id, "test-key", allowed_models=allowed_models, rpm=rpm, tpm=tpm,
        max_concurrent=max_concurrent, session_defaults=session_defaults,
        per_call_ceiling_nano=per_call,
    )  # fmt: skip
    made = await keys.create_key(spec, actor="test", now=NOW_MS)
    observer = Recorder()
    providers = build_providers(settings) | {
        "bedrock": Bedrock(lambda: AWS_EXAMPLE),
        "google": GoogleProvider(),
    } | dict(providers or {})  # fmt: skip
    # Through the server's own composition root; `pipeline_kwargs` may replace any argument.
    kwargs: dict[str, object] = {
        "ledger": ledger, "keys": keys, "sender": sender, "providers": providers,
        "now_ms": clock.now, "observer": observer, "metrics": metrics,
    } | (pipeline_kwargs or {})  # fmt: skip
    if wrap_ledger is not None:
        if kwargs["ledger"] is not ledger:  # it would wrap the stack's ledger, not the swapped one
            raise ValueError("give wrap_ledger or a swapped ledger in pipeline_kwargs, not both")
        kwargs["money"] = wrap_ledger(ledger)  # the pipeline's ledger; probes keep the real one
    gateway = build_gateway(settings, **kwargs)  # type: ignore[arg-type]
    pipeline, app = gateway.pipeline, gateway.data_app
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    )
    try:
        yield Stack(
            settings, pipeline, app, client, fake, clock, ledger, keys, made.secret, scope,
            cap, observer, gateway,
        )  # fmt: skip
    finally:
        await pipeline.aclose()
        await client.aclose()
        await sender.aclose()
        ledger.close()


def dedent(text: str) -> str:
    return textwrap.dedent(text)
