"""AWS Bedrock adapter: OpenAI chat <-> Converse / ConverseStream, signed with SigV4.

The client speaks OpenAI chat; the model id (or region-pinned inference profile such as
`eu.` or `global.`) and the region come from the Deployment, never from the client.
botocore is imported only inside the two functions that need it, so the base install and
`import spendgate.providers.bedrock` never load it (it ships in the optional `[bedrock]` extra).

Resolving the chain and refreshing a role's credentials are blocking network calls (instance
metadata, STS), so the async `prepare` fetches a snapshot in a worker thread and `translate` signs
with it. `prepare` takes a new snapshot once the last is `SNAPSHOT_SECONDS` old. botocore hands out
role credentials with at least its mandatory refresh window (10 minutes; it starts refreshing at
15) left, so a snapshot is valid for far longer than it is used. The assumption that remains: the
chain's credentials are not revoked early. A 401 or 403 from Bedrock drops the snapshot, so the
next call asks the chain again instead of reusing it.
"""

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Final
from urllib.parse import quote

from spendgate.core.cost import Usage
from spendgate.core.errors import ErrorCode, Refusal
from spendgate.providers.bedrock_request import converse_body
from spendgate.providers.bedrock_stream import (
    BedrockStream,
    as_obj,
    converse_usage,
    finish_reason,
    openai_usage,
)
from spendgate.providers.port import (
    AuthMode,
    CleanRequest,
    Deployment,
    Json,
    SseLimits,
    StreamRelay,
    Untranslatable,
    UpstreamError,
    UpstreamRequest,
    UpstreamResponse,
    held_ceiling,
    load_json,
    never_raises_error,
    never_raises_response,
    never_raises_usage,
    retry_after_seconds,
)
from spendgate.providers.refresh import Refreshes
from spendgate.providers.sse import asked_for_usage

SERVICE: Final = "bedrock"
CHAT: Final = "/v1/chat/completions"
_JSON_TYPE: Final = {"content-type": "application/json"}
_CONFIG_FAULTS = (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND)
SNAPSHOT_SECONDS: Final = 30  # well inside botocore's 10-minute mandatory refresh window
_SIGN_AT_MOST_SECONDS: Final = 300  # older means no `prepare` ran lately: `translate` asks inline
_REVOKED = (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN)
_CHAIN: Final = "aws"  # one credential source per adapter: one lock key


class _Silence(logging.Filter):
    """Drops every record: botocore's DEBUG output includes the signed session token."""

    def filter(self, record: logging.LogRecord) -> bool:
        return False


_SILENCE = _Silence()


class CredentialsUnavailable(RuntimeError):
    """No AWS credentials were found by the standard chain (the message holds no secret)."""


@dataclass(frozen=True, slots=True)
class AwsCredentials:
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    token: str | None = field(default=None, repr=False)


type CredentialsProvider = Callable[[], AwsCredentials]


def chain_credentials() -> CredentialsProvider:
    """Credentials from the standard chain (env, profile, task or instance role)."""
    import botocore.session  # noqa: PLC0415 - optional extra, loaded only when used

    resolved = botocore.session.get_session().get_credentials()

    def get() -> AwsCredentials:
        if resolved is None:
            raise CredentialsUnavailable("no AWS credentials found by the standard chain")
        frozen = resolved.get_frozen_credentials()  # a consistent snapshot while roles refresh
        if frozen.access_key is None or frozen.secret_key is None:
            raise CredentialsUnavailable("the AWS credentials are incomplete")
        return AwsCredentials(frozen.access_key, frozen.secret_key, frozen.token)

    return get


def sign(
    req: UpstreamRequest, creds: AwsCredentials, region: str, service: str = SERVICE
) -> UpstreamRequest:
    """`req` with `Authorization`, `X-Amz-Date` (and the session token) added by botocore."""
    from botocore.auth import SigV4Auth  # noqa: PLC0415 - optional extra
    from botocore.awsrequest import AWSRequest  # noqa: PLC0415
    from botocore.credentials import Credentials  # noqa: PLC0415

    logging.getLogger("botocore.auth").addFilter(_SILENCE)  # idempotent, thread-safe
    request = AWSRequest(method=req.method, url=req.url, data=req.body, headers=dict(req.headers))
    signer = SigV4Auth(
        Credentials(creds.access_key, creds.secret_key, creds.token), service, region
    )
    signer.add_auth(request)
    return UpstreamRequest(req.method, req.url, dict(request.headers.items()), req.body, req.stream)


def _message_parts(message: Mapping[str, Json]) -> tuple[str | None, list[Json]]:
    texts: list[str] = []
    calls: list[Json] = []
    content = message.get("content")
    for block in content if isinstance(content, list) else []:
        obj = as_obj(block)
        if isinstance(obj.get("text"), str):
            texts.append(str(obj["text"]))
        elif tool := as_obj(obj.get("toolUse")):
            calls.append(
                {
                    "id": tool.get("toolUseId"),
                    "type": "function",
                    "function": {
                        "name": tool.get("name"),
                        "arguments": json.dumps(tool.get("input") or {}, separators=(",", ":")),
                    },
                }
            )
    return ("".join(texts) if texts else None), calls


class Bedrock:
    """The Bedrock provider. Credentials are injected (or taken from the standard chain)."""

    name = "bedrock"

    def __init__(
        self,
        credentials: CredentialsProvider | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credentials = credentials  # None: resolve the standard chain on first use
        self._clock = clock
        self._snapshot: tuple[AwsCredentials, float] | None = None  # set only by `prepare`
        self._refreshes = Refreshes()

    def __repr__(self) -> str:
        return "Bedrock()"  # no credential provider, no state worth printing

    def validate(self, req: CleanRequest) -> Refusal | None:
        """Chat completions only; field refusals come from `translate`."""
        if req.endpoint != CHAT:
            return Refusal(
                ErrorCode.INVALID_REQUEST, "bedrock serves chat completions only", "endpoint"
            )
        return None

    async def prepare(self, deployment: Deployment) -> None:
        """Fetch a credentials snapshot in a worker thread unless a fresh one is cached."""
        if deployment.auth is AuthMode.SIGV4:
            stale = lambda: self._within(SNAPSHOT_SECONDS) is None  # noqa: E731 - one-line check
            await self._refreshes.ensure(_CHAIN, stale, self._take)

    def _within(self, seconds: float) -> AwsCredentials | None:
        """The snapshot if it is younger than `seconds`; a lookup that never blocks."""
        snapshot = self._snapshot
        if snapshot is None or self._clock() - snapshot[1] >= seconds:
            return None
        return snapshot[0]

    def _fetch(self) -> AwsCredentials:
        """Credentials from the provider (blocking: the chain may call the network)."""
        if self._credentials is None:
            self._credentials = chain_credentials()
        return self._credentials()

    def _take(self) -> None:
        self._snapshot = (self._fetch(), self._clock())

    def translate(self, req: CleanRequest, deployment: Deployment) -> UpstreamRequest:
        if deployment.auth is not AuthMode.SIGV4 or not deployment.region:
            raise ValueError("a bedrock deployment needs auth sigv4 and a region")
        if (refusal := self.validate(req)) is not None:
            raise Untranslatable(refusal.message, refusal.param)
        converse = converse_body(req.body, held_ceiling(req, "max_completion_tokens"))
        body = json.dumps(converse, separators=(",", ":")).encode()
        verb = "converse-stream" if req.stream else "converse"
        url = f"{deployment.base_url.rstrip('/')}/model/{quote(deployment.model, safe='')}/{verb}"
        unsigned = UpstreamRequest("POST", url, _JSON_TYPE, body, stream=req.stream)
        # What `prepare` readied (it renewed it if old); a direct caller without it fetches inline.
        creds = self._within(_SIGN_AT_MOST_SECONDS) or self._fetch()
        return sign(unsigned, creds, deployment.region)

    @never_raises_response
    def response(self, req: CleanRequest, resp: UpstreamResponse) -> UpstreamResponse:
        """A Converse answer as an OpenAI `chat.completion`; id, time and model come from `req`."""
        parsed = load_json(resp.body)
        if parsed is None:
            return UpstreamResponse(502, {}, b"")
        obj = as_obj(parsed)
        text, calls = _message_parts(as_obj(as_obj(obj.get("output")).get("message")))
        message: dict[str, Json] = {"role": "assistant", "content": text}
        if calls:
            message["tool_calls"] = calls
        out: dict[str, Json] = {
            "id": req.response_id,
            "object": "chat.completion",
            "created": req.created,
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason(obj.get("stopReason")),
                }
            ],
        }
        if (usage := converse_usage(obj.get("usage"))) is not None:
            out["usage"] = openai_usage(usage)
        body = json.dumps(out, separators=(",", ":")).encode()
        return UpstreamResponse(200, {"content-type": "application/json"}, body)

    def relay(self, req: CleanRequest, limits: SseLimits = SseLimits()) -> StreamRelay:  # noqa: B008
        """Decode one ConverseStream body into OpenAI chat SSE (and read its usage)."""
        include = asked_for_usage(req.body)
        return BedrockStream(
            req.model, req.response_id, req.created, include_usage=include, limits=limits
        )

    @never_raises_usage
    def usage(self, req: CleanRequest, response: UpstreamResponse) -> Usage | None:
        return converse_usage(as_obj(load_json(response.body)).get("usage"))

    @never_raises_error
    def error(self, response: UpstreamResponse) -> UpstreamError:
        kind = _error_type(response)
        status = response.status
        if status in _REVOKED:
            self._snapshot = None  # perhaps revoked or rotated: the next call asks the chain again
        if status == HTTPStatus.TOO_MANY_REQUESTS or kind in (
            "ThrottlingException",
            "ModelNotReadyException",
        ):
            code = ErrorCode.UPSTREAM_THROTTLED
        elif kind == "ValidationException" or status == HTTPStatus.BAD_REQUEST:
            code = ErrorCode.INVALID_REQUEST
        elif status in _CONFIG_FAULTS or kind in (
            "AccessDeniedException",
            "ResourceNotFoundException",
        ):
            code = ErrorCode.INTERNAL_ERROR  # the gateway's own credentials or model config
        else:  # ServiceUnavailable, InternalServer, ModelTimeout, ModelError, any other 5xx
            code = ErrorCode.UPSTREAM_UNAVAILABLE
        return UpstreamError(code, True, status, retry_after_seconds(response.headers))


def _error_type(response: UpstreamResponse) -> str:
    # `x-amzn-ErrorType: ThrottlingException:http://internal.amazon.com/...`
    for name, value in response.headers.items():
        if name.lower() == "x-amzn-errortype":
            return value.split(":", 1)[0]
    kind = as_obj(load_json(response.body)).get("__type")
    return kind.rsplit("#", 1)[-1] if isinstance(kind, str) else ""
