"""Stable stop reasons and attempt error codes (ADR-0022): httpx mapping, stream cuts, typing."""

import inspect
from typing import TYPE_CHECKING, cast

import httpx

from spendgate.core.failures import AttemptError, StopReason
from spendgate.core.outcomes import Outcome
from spendgate.ledger.port import AttemptRecord
from spendgate.pipeline import streaming
from spendgate.pipeline.streaming import StreamBody, cut_code
from spendgate.pipeline.transport import SendError, SendFailure, classify, read_body
from spendgate.providers.bedrock_stream import EventStreamError, FrameTooLarge
from spendgate.providers.port import StreamBroken, StreamLimitExceeded
from tests.integration.harness import async_test

# Every exception class httpx exports, with the code it is stored under. A new httpx release that
# adds one fails the completeness test below until it is listed here on purpose.
HTTPX_CODES: dict[type[Exception], AttemptError] = {
    httpx.ConnectError: AttemptError.CONNECT_ERROR,
    httpx.ConnectTimeout: AttemptError.CONNECT_TIMEOUT,
    httpx.PoolTimeout: AttemptError.POOL_TIMEOUT,
    httpx.ProxyError: AttemptError.PROXY_ERROR,
    httpx.UnsupportedProtocol: AttemptError.UNSUPPORTED_PROTOCOL,
    httpx.WriteError: AttemptError.WRITE_ERROR,
    httpx.WriteTimeout: AttemptError.WRITE_TIMEOUT,
    httpx.ReadError: AttemptError.READ_ERROR,
    httpx.ReadTimeout: AttemptError.READ_TIMEOUT,
    httpx.CloseError: AttemptError.CLOSE_ERROR,
    httpx.RemoteProtocolError: AttemptError.REMOTE_PROTOCOL_ERROR,
    httpx.LocalProtocolError: AttemptError.LOCAL_PROTOCOL_ERROR,
    httpx.DecodingError: AttemptError.DECODING_ERROR,
    httpx.TooManyRedirects: AttemptError.TOO_MANY_REDIRECTS,
    # Base classes and the status error: never raised by a send, but stored safely if they are.
    httpx.HTTPError: AttemptError.TRANSPORT_ERROR,
    httpx.RequestError: AttemptError.TRANSPORT_ERROR,
    httpx.TransportError: AttemptError.TRANSPORT_ERROR,
    httpx.TimeoutException: AttemptError.TRANSPORT_ERROR,
    httpx.NetworkError: AttemptError.TRANSPORT_ERROR,
    httpx.ProtocolError: AttemptError.TRANSPORT_ERROR,
    httpx.HTTPStatusError: AttemptError.TRANSPORT_ERROR,
    # Not HTTP client errors (programming or URL faults): the catch-all.
    httpx.InvalidURL: AttemptError.UNEXPECTED_ERROR,
    httpx.CookieConflict: AttemptError.UNEXPECTED_ERROR,
    httpx.StreamError: AttemptError.UNEXPECTED_ERROR,
    httpx.StreamConsumed: AttemptError.UNEXPECTED_ERROR,
    httpx.StreamClosed: AttemptError.UNEXPECTED_ERROR,
    httpx.ResponseNotRead: AttemptError.UNEXPECTED_ERROR,
    httpx.RequestNotRead: AttemptError.UNEXPECTED_ERROR,
}


def _httpx_exceptions() -> set[type[Exception]]:
    return {
        obj for obj in vars(httpx).values() if inspect.isclass(obj) and issubclass(obj, Exception)
    }


def test_every_httpx_exception_class_has_a_listed_code() -> None:
    assert _httpx_exceptions() == set(HTTPX_CODES)


def test_each_httpx_exception_is_stored_under_its_stable_code() -> None:
    for cls, code in HTTPX_CODES.items():
        exc = cls.__new__(cls)  # constructors differ; the mapping only looks at the class
        assert classify(exc).code is code, cls.__name__


def test_the_code_never_carries_the_exception_message() -> None:
    failure = classify(httpx.ConnectError("connect to secret-host.test failed"))
    assert failure.code is AttemptError.CONNECT_ERROR
    assert "secret-host" not in str(SendError(failure))


def test_an_exception_that_is_not_an_http_error_is_unexpected_error() -> None:
    for exc in (OSError("x"), RuntimeError("x"), ValueError("x")):
        assert classify(exc).code is AttemptError.UNEXPECTED_ERROR
        assert classify(exc).sent  # unknown means sent (M-13)


@async_test
async def test_a_buffered_answer_over_the_cap_is_response_too_large() -> None:
    response = httpx.Response(200, content=b"x" * 10)
    try:
        await read_body(response, max_bytes=4)
    except SendError as exc:
        assert exc.failure == SendFailure(True, False, AttemptError.RESPONSE_TOO_LARGE)
    else:
        raise AssertionError("an answer over the cap must fail")


def test_a_crossed_stream_limit_is_told_apart_from_bad_framing() -> None:
    assert cut_code(StreamLimitExceeded("x")) is AttemptError.STREAM_LIMIT_EXCEEDED
    assert cut_code(FrameTooLarge("x")) is AttemptError.STREAM_LIMIT_EXCEEDED  # both: the limit
    assert cut_code(StreamBroken("x")) is AttemptError.STREAM_MALFORMED
    assert cut_code(EventStreamError("x")) is AttemptError.STREAM_MALFORMED


def test_every_stop_reason_has_a_message_for_the_client() -> None:
    messages = streaming._STOP_MESSAGES  # the table itself is what is checked
    assert set(messages) == set(StopReason)
    assert all(m.startswith("The stream was stopped because") for m in messages.values())


def test_mypy_rejects_a_stop_reason_or_error_code_outside_the_enums() -> None:
    """Checked by `make type`, not at run time: mypy --strict runs on tests with
    `warn_unused_ignores`, so if any of these lines ever type-checked its ignore would be unused
    and the gate would fail. Before ADR-0022 every one of them was accepted."""
    if TYPE_CHECKING:
        cast(StreamBody, None).abort("lease_gone")  # type: ignore[arg-type]
        SendFailure(sent=True, timeout=True, code="ReadTimeout")  # type: ignore[arg-type]
        AttemptRecord(1, "p", "d", "x", Outcome.TIMEOUT, 0, 0, 0, error_code="ReadTimeout")  # type: ignore[arg-type]
    assert StopReason("lease_lost") is StopReason.LEASE_LOST
