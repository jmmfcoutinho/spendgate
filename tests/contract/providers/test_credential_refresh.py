"""`Provider.prepare`: token refreshes run in a worker thread, once per credential at a time.

Each fake refresh blocks on a `Gate` (tests/fakes/gate.py) the test opens: no real sleeps.
"""

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from spendgate.providers.azure_openai import AzureOpenAIProvider
from spendgate.providers.azure_openai_auth import V1_SCOPE
from spendgate.providers.azure_speech import AzureSpeech
from spendgate.providers.bedrock import SNAPSHOT_SECONDS, AwsCredentials, Bedrock
from spendgate.providers.google import GoogleProvider
from spendgate.providers.port import (
    AuthMode,
    CleanRequest,
    Deployment,
    Provider,
    UpstreamResponse,
)
from tests.contract.providers.azure_speech.helpers import deployment as speech_deployment
from tests.contract.providers.azure_speech.helpers import request as speech_request
from tests.contract.providers.bedrock.helpers import EXAMPLE
from tests.fakes import BASE_URL
from tests.fakes.gate import GATE_TIMEOUT_S, Gate
from tests.unit.test_provider_port import ADAPTERS

pytestmark = pytest.mark.anyio

TOKEN = "fake-refreshed-token-0000"  # noqa: S105  # pragma: allowlist secret
CHAT = CleanRequest(
    "/v1/chat/completions",
    "gateway-name",
    {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
    output_ceiling=5,
)


@dataclass(frozen=True)
class Token:
    token: str
    expires_on: int


class EntraCredential:
    def __init__(self, gate: Gate) -> None:
        self._gate = gate

    def get_token(self, *scopes: str) -> Token:
        self._gate.refresh()
        return Token(TOKEN, 2**40)  # far future: the cache keeps it


class VertexCredentials:
    def __init__(self, gate: Gate) -> None:
        self._gate = gate
        self.token: str | None = None
        self.project_id: str | None = "demo-project"

    @property
    def valid(self) -> bool:
        return self.token is not None

    def refresh(self, request: object) -> None:
        self._gate.refresh()
        self.token = TOKEN


def aws(gate: Gate) -> Callable[[], AwsCredentials]:
    def get() -> AwsCredentials:
        gate.refresh()
        return EXAMPLE

    return get


type Case = tuple[Provider, Deployment, CleanRequest]


def azure(gate: Gate) -> Case:
    dep = Deployment("azure_openai", BASE_URL, "prod-alpha", AuthMode.BEARER_TOKEN)
    return AzureOpenAIProvider(credential=EntraCredential(gate)), dep, CHAT


def speech(gate: Gate) -> Case:
    def source() -> Token:
        gate.refresh()
        return Token(TOKEN, 2**40)

    return AzureSpeech(source), speech_deployment(auth=AuthMode.BEARER_TOKEN), speech_request()


def vertex(gate: Gate) -> Case:
    creds = VertexCredentials(gate)
    dep = Deployment(
        "google", BASE_URL, "gemini-4-carbon", AuthMode.BEARER_TOKEN, region="europe-west4",
        credential='{"type": "service_account"}',  # pragma: allowlist secret
    )  # fmt: skip
    return GoogleProvider(lambda _: creds), dep, CHAT


BEDROCK = Deployment("bedrock", BASE_URL, "eu.model-v1:0", AuthMode.SIGV4, region="eu-west-1")


def bedrock(gate: Gate) -> Case:
    return Bedrock(aws(gate)), BEDROCK, CHAT


CASES = [azure, speech, vertex, bedrock]


async def opened_once_entered(gate: Gate) -> None:
    """Wait (in a thread, bounded) until the refresh is blocked, then let it finish."""
    assert await asyncio.to_thread(gate.entered.wait, GATE_TIMEOUT_S)
    gate.opened.set()


@pytest.mark.parametrize("build", CASES, ids=lambda b: b.__name__)
async def test_a_blocking_refresh_runs_in_a_worker_thread_while_other_coroutines_progress(
    build: Callable[[Gate], Case],
) -> None:
    gate = Gate()
    provider, dep, req = build(gate)
    refreshing = asyncio.create_task(provider.prepare(dep))
    assert await asyncio.to_thread(gate.entered.wait, GATE_TIMEOUT_S)
    assert await asyncio.create_task(_other()) == "ran"  # the loop is free meanwhile
    assert not refreshing.done() and not gate.opened.is_set()
    gate.opened.set()
    await refreshing
    assert gate.threads[0] != threading.get_ident()  # not the event loop's thread
    up = provider.translate(req, dep)  # the token is cached: no refresh inline
    assert gate.calls == 1
    assert TOKEN in str(up.headers) or "AKIDEXAMPLE" in str(up.headers)


async def _other() -> str:
    await asyncio.sleep(0)  # a bare yield to the loop, not a timed wait
    return "ran"


@pytest.mark.parametrize("build", CASES, ids=lambda b: b.__name__)
async def test_concurrent_calls_share_one_refresh(
    build: Callable[[Gate], Case],
) -> None:
    gate = Gate()
    provider, dep, req = build(gate)
    calls = [provider.prepare(dep) for _ in range(8)]
    await asyncio.gather(*calls, opened_once_entered(gate))
    assert gate.calls == 1
    for _ in range(3):
        provider.translate(req, dep)
    assert gate.calls == 1


@pytest.mark.parametrize("build", CASES, ids=lambda b: b.__name__)
async def test_a_failed_refresh_raises_and_the_next_call_tries_again(
    build: Callable[[Gate], Case],
) -> None:
    gate = Gate(fail=True)
    gate.opened.set()
    provider, dep, _ = build(gate)
    with pytest.raises(RuntimeError):
        await provider.prepare(dep)  # the pipeline maps this to internal_error, nothing sent
    gate.fail = False
    await provider.prepare(dep)  # a failure is never cached: fail closed, then recover
    assert gate.calls == 2


@pytest.mark.parametrize("build", CASES, ids=lambda b: b.__name__)
async def test_a_cancelled_caller_leaves_its_refresh_running_for_the_next_one(
    build: Callable[[Gate], Case],
) -> None:
    gate = Gate()
    provider, dep, _ = build(gate)
    first = asyncio.create_task(provider.prepare(dep))
    assert await asyncio.to_thread(gate.entered.wait, GATE_TIMEOUT_S)
    first.cancel()  # the client went away mid-refresh
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(provider.prepare(dep))  # joins the refresh still in flight
    await _other()
    gate.opened.set()
    await second
    assert gate.calls == 1


async def test_an_entra_refresh_for_one_scope_never_blocks_a_translate_for_another() -> None:
    gate = Gate()
    credential = OneSlowScope(gate, V1_SCOPE)
    provider = AzureOpenAIProvider(credential=credential)
    classic = Deployment(
        "azure_openai", BASE_URL, "prod-alpha", AuthMode.BEARER_TOKEN, api_version="2024-10-21"
    )
    v1 = Deployment("azure_openai", BASE_URL, "prod-alpha", AuthMode.BEARER_TOKEN)
    await provider.prepare(classic)  # scope A cached
    slow = asyncio.create_task(provider.prepare(v1))  # scope B: blocked in its worker thread
    assert await asyncio.to_thread(gate.entered.wait, GATE_TIMEOUT_S)
    await provider.prepare(classic)
    up = provider.translate(CHAT, classic)  # on the loop: must not wait for scope B
    assert not gate.timed_out and not slow.done()
    assert up.headers["Authorization"] == f"Bearer {TOKEN}"
    gate.opened.set()
    await slow


class OneSlowScope:
    """An Entra credential whose fetch for one scope blocks on the gate."""

    def __init__(self, gate: Gate, slow: str) -> None:
        self._gate, self._slow = gate, slow

    def get_token(self, *scopes: str) -> Token:
        if self._slow in scopes:
            self._gate.refresh()
        return Token(TOKEN, 2**40)


async def test_a_bedrock_snapshot_is_renewed_by_prepare_and_used_by_translate() -> None:
    gate = Gate()
    gate.opened.set()
    now = [0.0]
    provider = Bedrock(aws(gate), clock=lambda: now[0])
    await provider.prepare(BEDROCK)
    now[0] = SNAPSHOT_SECONDS - 1
    await provider.prepare(BEDROCK)
    assert gate.calls == 1
    now[0] = SNAPSHOT_SECONDS
    provider.translate(CHAT, BEDROCK)  # signs with what prepare readied: no inline fetch
    assert gate.calls == 1
    await provider.prepare(BEDROCK)  # old enough: renewed off the loop
    assert gate.calls == 2
    now[0] = 10_000  # no prepare for a long time (a direct caller): fetched inline
    provider.translate(CHAT, BEDROCK)
    assert gate.calls == 3


@pytest.mark.parametrize("status", [401, 403])
async def test_a_bedrock_auth_failure_drops_the_snapshot(status: int) -> None:
    gate = Gate()
    gate.opened.set()
    provider = Bedrock(aws(gate), clock=lambda: 0.0)
    await provider.prepare(BEDROCK)
    provider.error(UpstreamResponse(status, {}, b""))
    await provider.prepare(BEDROCK)  # asks the chain again instead of reusing it
    assert gate.calls == 2


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.name)
async def test_static_keys_need_no_preparation(adapter: Provider) -> None:
    fake = "fake-key-0000"  # pragma: allowlist secret
    dep = Deployment(adapter.name, BASE_URL, "m", AuthMode.API_KEY, credential=fake)
    await adapter.prepare(dep)  # returns at once: no thread, no lock, no credential call
