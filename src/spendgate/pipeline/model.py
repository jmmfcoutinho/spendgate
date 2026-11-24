"""Plain data and ports of the request pipeline: endpoints, replies, observer events.

No HTTP framework here. The gateway turns an `Inbound` into a call to `Pipeline` and a `Reply`
back into a Starlette response; library mode can do the same without Starlette.
"""

import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from spendgate.core.cost import Usage
from spendgate.core.errors import ErrorCode, Refusal, Shape
from spendgate.core.estimate import RequestKind
from spendgate.core.money import Money
from spendgate.core.outcomes import Outcome
from spendgate.ledger.key_types import Principal
from spendgate.pipeline.faults import InternalEvent
from spendgate.providers.port import Json

type Clock = Callable[[], int]  # epoch milliseconds
type IdGenerator = Callable[[], str]


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One data-plane route: its protocol path, what it prices and which error shape it uses."""

    path: str
    kind: RequestKind
    shape: Shape = "openai"
    multipart: bool = False


CHAT = Endpoint("/v1/chat/completions", RequestKind.CHAT)
RESPONSES = Endpoint("/v1/responses", RequestKind.RESPONSES)
EMBEDDINGS = Endpoint("/v1/embeddings", RequestKind.EMBEDDING)
TRANSCRIPTIONS = Endpoint("/v1/audio/transcriptions", RequestKind.TRANSCRIPTION, multipart=True)
SPEECH = Endpoint("/v1/audio/speech", RequestKind.SPEECH)
MESSAGES = Endpoint("/v1/messages", RequestKind.MESSAGES, "anthropic")


def models_endpoint(shape: Shape) -> Endpoint:
    """`GET /v1/models` has no priced kind; CHAT is a placeholder that nothing reads."""
    return Endpoint("/v1/models", RequestKind.CHAT, shape)


ENDPOINTS = {e.path: e for e in (CHAT, RESPONSES, EMBEDDINGS, TRANSCRIPTIONS, SPEECH, MESSAGES)}


@dataclass(frozen=True, slots=True)
class Inbound:
    """A request after step 1: size-checked and parsed (JSON object, or multipart fields+files)."""

    endpoint: Endpoint
    headers: Mapping[str, str]  # lower-case names
    body: Mapping[str, Json] = field(repr=False)
    files: Mapping[str, bytes] = field(default_factory=dict, repr=False)


class ByteStream(Protocol):
    """A live relayed stream. The adapter iterates it and MUST call `aclose()` when it stops
    (done, client gone, error): that is what aborts the upstream call and settles the hold."""

    def __aiter__(self) -> "ByteStream": ...

    async def __anext__(self) -> bytes: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Reply:
    """What the HTTP adapter sends: a buffered `body`, or a live `stream`."""

    status: int
    headers: Mapping[str, str]
    body: bytes = field(default=b"", repr=False)
    stream: ByteStream | None = field(default=None, repr=False)


class Sender(Protocol):
    """The one HTTP call the pipeline makes; `httpx.AsyncClient` satisfies it."""

    async def send(self, request: httpx.Request, *, stream: bool = False) -> httpx.Response: ...


class KeyVerifier(Protocol):
    """`ledger.keys.KeyService` as the pipeline sees it."""

    async def verify_api(self, raw: str, now: int) -> Principal | Refusal: ...


# --- observer ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefusedEvent:
    request_id: str
    code: ErrorCode
    endpoint: str
    model: str | None = None  # the routed catalog id; None when the body names no routed model
    key_id: str | None = None


@dataclass(frozen=True, slots=True)
class HeldEvent:
    request_id: str
    model: str
    hold: Money


@dataclass(frozen=True, slots=True)
class AttemptEvent:
    request_id: str
    model: str
    provider: str
    deployment: str
    outcome: Outcome
    status: int | None
    not_processed: bool
    latency_ms: int
    cooldown_ms: int | None = None  # set when the attempt put the deployment into cooldown


@dataclass(frozen=True, slots=True)
class SettledEvent:
    request_id: str
    key_id: str
    scope_id: str
    endpoint: str
    model: str
    deployment: str
    outcome: Outcome
    charged: Money
    hold: Money
    usage: Usage | None
    latency_ms: int
    released: bool  # True: the hold was given back (every attempt was not_processed)
    remaining: Money | None = None  # room left in the tightest budget after this settlement
    scope_path: str | None = None
    review_required: bool = False  # C-22: absurd reported usage, booked in full and flagged
    ttft_ms: int | None = None  # streams: request start to the first byte sent to the client
    http_status: int | None = None  # the provider's status on the last attempt; None: no answer


class Observer(Protocol):
    """Receives facts, never decisions. Implemented by gateway-side metrics and logs.

    Methods must not raise; the pipeline ignores an observer that does, because money handling
    must never depend on a metrics bug.
    """

    def on_refused(self, event: RefusedEvent) -> None: ...

    def on_held(self, event: HeldEvent) -> None: ...

    def on_attempt(self, event: AttemptEvent) -> None: ...

    def on_settled(self, event: SettledEvent) -> None: ...

    def on_internal(self, event: InternalEvent) -> None: ...


class NoopObserver:
    def on_refused(self, event: RefusedEvent) -> None:
        pass

    def on_held(self, event: HeldEvent) -> None:
        pass

    def on_attempt(self, event: AttemptEvent) -> None:
        pass

    def on_settled(self, event: SettledEvent) -> None:
        pass

    def on_internal(self, event: InternalEvent) -> None:
        pass


def report_internal(observer: Observer | None, event: InternalEvent) -> None:
    """Hand a redacted trace to the observer; a failing (or older) observer changes nothing."""
    if observer is not None:
        with contextlib.suppress(Exception):
            observer.on_internal(event)
