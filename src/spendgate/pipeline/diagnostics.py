"""The one log record an unexpected exception leaves behind at a catch-all boundary.

The structured call log is an allowlist (`gateway.logs`) and drops `exc_info` on purpose: a
provider body, a prompt or a key can end up inside an exception message. A programming error
still has to be traceable for the operator, so the record carries the exception class and the
source frames only — file, line — and never the message, the arguments or a local.

A catch-all that turns an unknown exception into `internal_error` calls `report_internal`. A
domain error (`Refusal`, `Untranslatable`, `StreamBroken`) never reaches it: it has its own
`except` clause and is expected behaviour, not a bug.
"""

import logging
import os

logger = logging.getLogger("spendgate.internal")
FIELD_ATTR = "spendgate_fields"  # the mapping the JSON formatter reads (gateway.logs)
FRAME_LIMIT = 32  # a runaway recursion chain is truncated; these frames already name the cause


def frames_of(exc: BaseException) -> str:
    """`file.py:line` per traceback frame, outermost first; never the exception text."""
    frames: list[str] = []
    tb = exc.__traceback__
    while tb is not None:
        if len(frames) == FRAME_LIMIT:
            frames.append("...")
            break
        frames.append(f"{os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}")
        tb = tb.tb_next
    return " <- ".join(frames)


def report_internal(exc: BaseException) -> None:
    """Log one redacted `internal_error` event: exception class plus `file:line` frames."""
    error_type = type(exc).__qualname__
    frames = frames_of(exc)
    logger.error(
        "internal_error: %s at %s",
        error_type,
        frames,
        extra={FIELD_ATTR: {"event": "internal_error", "error_type": error_type, "frames": frames}},
    )