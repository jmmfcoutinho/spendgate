"""Stable codes for why an attempt failed or a stream was stopped: the `calls.error_code` values.

The column used to collect free strings (exception class names among them), which changed with a
library upgrade and could not be filtered on. Every value written now comes from one of the enums
below or from `ErrorCode`/`Outcome`, so mypy rejects anything else; data-model.md lists them all.
"""

from enum import StrEnum

from spendgate.core.errors import ErrorCode
from spendgate.core.outcomes import Outcome


class StopReason(StrEnum):
    """Why the gateway stopped a stream that had already started (protocol 2.8)."""

    LEASE_EXPIRING = "lease_expiring"  # the stream's lease is within one heartbeat of expiry
    LEASE_LOST = "lease_lost"  # the renewal did not return it: a sweeper expired the hold
    RENEWAL_FAILED = "renewal_failed"  # the renewal itself failed: nobody knows the lease
    MAX_DURATION = "max_duration"  # longer than `server.max_request_seconds` (SR-13)
    KILL_SWITCH = "kill_switch"
    SESSION_TIME = "session_time"  # the session's `max_seconds` passed
    CLIENT_GONE = "client_gone"  # the client disconnected or closed the body early


class AttemptError(StrEnum):
    """Why one upstream attempt failed on the wire or inside the gateway.

    Not a provider's own error: a provider answer is recorded with its `ErrorCode`.
    """

    # Never sent (`not_processed`): the connection was not set up.
    CONNECT_ERROR = "connect_error"
    CONNECT_TIMEOUT = "connect_timeout"
    POOL_TIMEOUT = "pool_timeout"
    PROXY_ERROR = "proxy_error"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    # Sent: the provider may have done the work.
    WRITE_ERROR = "write_error"
    WRITE_TIMEOUT = "write_timeout"
    READ_ERROR = "read_error"
    READ_TIMEOUT = "read_timeout"
    CLOSE_ERROR = "close_error"
    REMOTE_PROTOCOL_ERROR = "remote_protocol_error"
    LOCAL_PROTOCOL_ERROR = "local_protocol_error"
    DECODING_ERROR = "decoding_error"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    TRANSPORT_ERROR = "transport_error"  # any other HTTP client error
    UNEXPECTED_ERROR = "unexpected_error"  # an exception that is not an HTTP client error
    RESPONSE_TOO_LARGE = "response_too_large"  # a buffered answer over the size cap
    # A started stream the provider side broke.
    STREAM_MALFORMED = "stream_malformed"  # bad framing, CRC, encoding or an event cut short
    STREAM_LIMIT_EXCEEDED = "stream_limit_exceeded"  # an SSE or frame size limit (C-22)
    # The gateway could not handle a sent request's answer: charged at the full hold (CR-01).
    RELAY_ERROR = "relay_error"
    ADAPTER_ERROR = "adapter_error"


# The values the pipeline may write to `calls.error_code`. A provider's error answer records its
# `ErrorCode`; an attempt the gateway ended while in flight (cancel, deadline, internal error)
# records its `Outcome`.
type CallErrorCode = StopReason | AttemptError | ErrorCode | Outcome
