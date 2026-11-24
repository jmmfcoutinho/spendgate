"""Structured JSON call logs with an allowlist redaction guard.

One JSON line per call. The guard is an allowlist, not a denylist: a field nobody has
reviewed (a prompt, a header, a secret pasted into an exception message) is dropped, not
scrubbed. String values are also checked for key-secret shapes as a second net.
"""

import json
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime

from spendgate.core.money import Money
from spendgate.core.outcomes import Outcome
from spendgate.pipeline.faults import InternalEvent

# The only fields that may reach a log line. Adding one is a reviewed change.
ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "event", "request_id", "key_prefix", "scope_path", "model", "deployment", "outcome",
        "input_tokens", "output_tokens", "cached_tokens", "cost", "currency", "latency_ms",
        "ttft_ms", "status", "http_status", "reason", "level", "ts", "logger",
        "where", "error_class", "frames", "settled",
    }
)  # fmt: skip

# Provider-style and spendgate-style secrets: sg-..., sk-..., Bearer tokens.
# Hyphen only: key secrets are sg-/sga-/sk-; request ids (sg_req_...) must stay in the logs.
_SECRET_SHAPE = re.compile(r"(?i)\b(?:sga?|sk)-[a-z0-9_-]{8,}|bearer\s+\S+")
KEY_PREFIX_LEN = 8
_EXTRA_ATTR = "spendgate_fields"
LOGGER_NAME = "spendgate.call"
INTERNAL_EVENT = "spendgate.internal"


def redact(fields: Mapping[str, object]) -> dict[str, object]:
    """Keep allowlisted, scalar, secret-free fields; drop everything else."""
    kept: dict[str, object] = {}
    for name, value in fields.items():
        if name not in ALLOWED_FIELDS:
            continue
        if not isinstance(value, str | int | float | bool | None):
            continue
        if isinstance(value, str) and _SECRET_SHAPE.search(value):
            continue
        kept[name] = value
    return kept


class JsonFormatter(logging.Formatter):
    """Formats only the allowlisted structured fields; the free-text message is never used."""

    def format(self, record: logging.LogRecord) -> str:
        raw = getattr(record, _EXTRA_ATTR, None)
        fields: dict[str, object] = dict(raw) if isinstance(raw, Mapping) else {}
        fields.setdefault("ts", datetime.fromtimestamp(record.created, UTC).isoformat())
        fields.setdefault("level", record.levelname)
        # record.getMessage() and exc_info are deliberately not emitted: they may hold bodies.
        return json.dumps(redact(fields), sort_keys=True, separators=(",", ":"))


def log_event(
    logger: logging.Logger, event: str, level: int = logging.INFO, **fields: object
) -> None:
    """Emit one structured event; unknown fields are dropped by the formatter's guard."""
    logger.log(level, event, extra={_EXTRA_ATTR: {"event": event, **fields}})


def log_call(  # noqa: PLR0913 - the documented call-log fields, all keyword-only
    logger: logging.Logger, *, request_id: str, key_prefix_: str, scope_path: str, model: str,
    deployment: str, outcome: Outcome, input_tokens: int, output_tokens: int, cost: Money,
    latency_ms: float, ttft_ms: float | None = None, cached_tokens: int = 0,
    http_status: int | None = None,
) -> None:  # fmt: skip
    """The one-line record of a finished call (cost as a decimal string plus currency)."""
    amount = cost.format().rsplit(" ", 1)[0]
    log_event(
        logger, "call", request_id=request_id, key_prefix=key_prefix_[:KEY_PREFIX_LEN],
        scope_path=scope_path, model=model, deployment=deployment, outcome=outcome.value,
        input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached_tokens,
        cost=amount, currency=cost.currency, latency_ms=latency_ms, ttft_ms=ttft_ms,
        http_status=http_status,
    )  # fmt: skip


def log_internal(logger: logging.Logger, event: InternalEvent) -> None:
    """A catch-all's redacted trace: class and frames only, never the exception's message."""
    fault = event.fault
    log_event(
        logger, INTERNAL_EVENT, logging.ERROR, request_id=event.request_id, where=fault.where,
        error_class=fault.error_class, frames=fault.frames_text, settled=event.settled,
    )  # fmt: skip


def configure_json_logging(stream: logging.Handler | None = None) -> logging.Logger:
    """Attach the JSON formatter to the call logger (idempotent) and return it."""
    logger = logging.getLogger(LOGGER_NAME)
    handler = stream or logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
