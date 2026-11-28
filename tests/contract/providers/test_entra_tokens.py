"""The Entra token cache shared by both Azure adapters (pen test SG-PT-15, issue #140).

T-IDN-01 a provider 401 or 403 drops the cached token; T-IDN-02 `translate` never asks the token
source; T-IDN-03 a token whose expiry is not in the future is refused and nothing is sent; T-IDN-04
a short-lived token is reused for half its lifetime; T-IDN-05 concurrent calls fetch once. The
clock is injected and the credential is a fake: no network, no sleep.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from spendgate.providers.azure_openai import AzureOpenAIProvider
from spendgate.providers.azure_openai_auth import (
    REFRESH_MARGIN_SECONDS,
    ExpiredTokenError,
    TokenNotReadyError,
)
from spendgate.providers.azure_speech import AzureSpeech
from spendgate.providers.port import (
    AuthMode,
    CleanRequest,
    Deployment,
    Provider,
    UpstreamResponse,
)
from tests.contract.providers.azure_speech.helpers import deployment as speech_deployment
from tests.contract.providers.azure_speech.helpers import request as speech_request
from tests.fakes import BASE_URL
from tests.fakes.gate import GATE_TIMEOUT_S, Gate

pytestmark = pytest.mark.anyio

TOKEN = "fake-entra-token"  # noqa: S105  # pragma: allowlist secret
START = 1_000_000.0
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


@dataclass
class Source:
    """A fake Entra credential on the test's clock: each fetch is a new token of `lifetime`."""

    now: list[float] = field(default_factory=lambda: [START])
    lifetime: float = 3600
    gate: Gate | None = None
    calls: int = 0

    def clock(self) -> float:
        return self.now[0]

    def get_token(self, *scopes: str) -> Token:
        if self.gate is not None:
            self.gate.refresh()
        self.calls += 1
        return Token(f"{TOKEN}-{self.calls}", int(self.now[0] + self.lifetime))


type Case = tuple[Provider, Deployment, CleanRequest]


def openai(src: Source) -> Case:
    dep = Deployment("azure_openai", BASE_URL, "prod-alpha", AuthMode.BEARER_TOKEN)
    return AzureOpenAIProvider(credential=src, clock=src.clock), dep, CHAT


def speech(src: Source) -> Case:
    adapter = AzureSpeech(src.get_token, clock=src.clock)
    return adapter, speech_deployment(auth=AuthMode.BEARER_TOKEN), speech_request()


CASES = [openai, speech]


def bearer(provider: Provider, dep: Deployment, req: CleanRequest) -> str:
    return provider.translate(req, dep).headers["Authorization"].removeprefix("Bearer ")


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("build", CASES, ids=lambda b: b.__name__)
async def test_t_idn_01_an_azure_401_drops_the_cached_entra_token_and_the_next_call_fetches_one(
    build: Callable[[Source], Case], status: int
) -> None:
    src = Source()
    provider, dep, req = build(src)
    await provider.prepare(dep)
    assert bearer(provider, dep, req) == f"{TOKEN}-1"
    provider.error(UpstreamResponse(status, {}, b'{"error":{"code":"401"}}'))
    with pytest.raises(TokenNotReadyError):
        provider.translate(req, dep)  # the rejected token is never sent again
    await provider.prepare(dep)  # off the loop, as for a cold cache
    assert bearer(provider, dep, req) == f"{TOKEN}-2"
    assert src.calls == 2


@pytest.mark.parametrize("build", CASES, ids=lambda b: b.__name__)
async def test_any_other_error_keeps_the_token(build: Callable[[Source], Case]) -> None:
    src = Source()
    provider, dep, req = build(src)
    await provider.prepare(dep)
    for status in (400, 404, 429, 500):
        provider.error(UpstreamResponse(status, {}, b""))
    await provider.prepare(dep)
    assert bearer(provider, dep, req) == f"{TOKEN}-1"
    assert src.calls == 1


@pytest.mark.parametrize("build", CASES, ids=lambda b: b.__name__)
async def test_t_idn_02_translate_never_calls_the_token_source(
    build: Callable[[Source], Case],
) -> None:
    src = Source()
    provider, dep, req = build(src)
    with pytest.raises(TokenNotReadyError):
        provider.translate(req, dep)  # no `prepare`: fail closed, not a fetch on the loop
    assert src.calls == 0
    await provider.prepare(dep)
    src.now[0] += src.lifetime  # expired, and no `prepare` since
    with pytest.raises(TokenNotReadyError):
        provider.translate(req, dep)
    assert src.calls == 1


@pytest.mark.parametrize("left", [0, -60])
@pytest.mark.parametrize("build", CASES, ids=lambda b: b.__name__)
async def test_t_idn_03_an_expired_token_from_the_source_is_refused_nothing_is_sent(
    build: Callable[[Source], Case], left: float
) -> None:
    src = Source(lifetime=left)  # clock skew or a broken identity endpoint
    provider, dep, req = build(src)
    with pytest.raises(ExpiredTokenError):
        await provider.prepare(dep)  # the pipeline answers internal_error and logs the class
    with pytest.raises(TokenNotReadyError):
        provider.translate(req, dep)  # never cached: no request is built, none is sent
    assert src.calls == 1  # refused once, not fetched again inline
    with pytest.raises(ExpiredTokenError):
        await provider.prepare(dep)  # the next call asks again
    src.lifetime = 3600
    await provider.prepare(dep)  # and recovers once the source is sound
    assert bearer(provider, dep, req) == f"{TOKEN}-3"


@pytest.mark.parametrize("build", CASES, ids=lambda b: b.__name__)
async def test_t_idn_04_a_short_lived_token_is_reused_for_half_its_lifetime(
    build: Callable[[Source], Case],
) -> None:
    src = Source(lifetime=200)  # under the 300 s margin: it used to be fetched on every call
    provider, dep, req = build(src)
    for _ in range(5):
        await provider.prepare(dep)
        bearer(provider, dep, req)
    src.now[0] = START + 99
    await provider.prepare(dep)
    assert bearer(provider, dep, req) == f"{TOKEN}-1"
    src.now[0] = START + 100  # half way: refreshed
    await provider.prepare(dep)
    assert bearer(provider, dep, req) == f"{TOKEN}-2"
    assert src.calls == 2


@pytest.mark.parametrize("build", CASES, ids=lambda b: b.__name__)
async def test_a_long_lived_token_is_refreshed_at_the_five_minute_margin(
    build: Callable[[Source], Case],
) -> None:
    src = Source(lifetime=3600)
    provider, dep, req = build(src)
    await provider.prepare(dep)
    src.now[0] = START + 3600 - REFRESH_MARGIN_SECONDS - 1
    await provider.prepare(dep)
    assert src.calls == 1
    src.now[0] = START + 3600 - REFRESH_MARGIN_SECONDS
    await provider.prepare(dep)
    assert bearer(provider, dep, req) == f"{TOKEN}-2"


@pytest.mark.parametrize("build", CASES, ids=lambda b: b.__name__)
async def test_t_idn_05_concurrent_calls_still_fetch_once(
    build: Callable[[Source], Case],
) -> None:
    gate = Gate()
    src = Source(lifetime=200, gate=gate)
    provider, dep, req = build(src)

    async def open_once_entered() -> None:
        assert await asyncio.to_thread(gate.entered.wait, GATE_TIMEOUT_S)
        gate.opened.set()

    await asyncio.gather(*(provider.prepare(dep) for _ in range(8)), open_once_entered())
    assert gate.calls == 1  # a short-lived token: one fetch for the burst
    provider.error(UpstreamResponse(401, {}, b""))
    gate.entered.clear()
    gate.opened.clear()
    await asyncio.gather(*(provider.prepare(dep) for _ in range(8)), open_once_entered())
    assert gate.calls == 2  # after a 401: one fetch again, not one per caller
    assert {bearer(provider, dep, req) for _ in range(3)} == {f"{TOKEN}-2"}
