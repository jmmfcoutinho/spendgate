"""Issue #14: an unexpected exception at a catch-all leaves a redacted log record.

The client still gets `500 internal_error` with no detail. The operator gets one ERROR record:
the exception class and the `file:line` frames, never the exception message, its arguments or a
local (a provider body or a prompt can sit inside a `KeyError`). A domain error keeps its own
`except` clause and is not an internal error, so it logs nothing.
"""

import json
import logging
from pathlib import Path
from typing import cast

import pytest

from spendgate.gateway.logs import JsonFormatter
from spendgate.pipeline import diagnostics
from spendgate.providers.openai import OpenAIProvider
from spendgate.providers.port import Untranslatable
from tests.fakes import Script
from tests.integration.harness import async_test, open_stack

INTERNAL_MESSAGES = {
    "The gateway failed; the request was not completed.",
    "The provider answer could not be read.",
    "The upstream request could not be built.",
    "The stream could not be relayed.",
}
LEAK = "sk-live-LEAKEDSECRET0000"  # noqa: S105 - fake fixture, must never reach a log


def internal_events(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if getattr(record, diagnostics.FIELD_ATTR, {}).get("event") == "internal_error"
    ]


def fields_of(record: logging.LogRecord) -> dict[str, object]:
    return cast(dict[str, object], getattr(record, diagnostics.FIELD_ATTR))


def leaky(*_args: object, **_kwargs: object) -> object:
    raise KeyError(f"adapter bug while reading {LEAK}")


def domain(*_args: object, **_kwargs: object) -> object:
    raise Untranslatable("field is not supported", "messages")


@async_test
async def test_unexpected_exception_is_logged_redacted_and_the_client_sees_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(OpenAIProvider, "response", leaky)
    caplog.set_level(logging.ERROR, logger="spendgate.internal")
    async with open_stack(tmp_path) as s:
        r = await s.chat()
    assert r.status_code == 500
    error = r.json()["error"]
    assert error["code"] == "internal_error"
    assert error["message"] in INTERNAL_MESSAGES
    assert "KeyError" not in r.text and LEAK not in r.text

    (record,) = internal_events(caplog)
    assert record.levelno == logging.ERROR
    fields = fields_of(record)
    assert fields["error_type"] == "KeyError"
    frames = fields["frames"]
    assert isinstance(frames, str)
    assert "routing.py:" in frames  # the frame that called the adapter
    assert record.getMessage().startswith("internal_error: KeyError at ")
    assert LEAK not in record.getMessage() and LEAK not in caplog.text


def test_the_json_formatter_keeps_the_frames_and_drops_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="spendgate.internal")
    try:
        raise KeyError(f"prompt text {LEAK}")
    except KeyError as exc:
        diagnostics.report_internal(exc)
    (record,) = internal_events(caplog)
    line = JsonFormatter().format(record)
    body = json.loads(line)
    assert body["event"] == "internal_error"
    assert body["error_type"] == "KeyError"
    assert ".py:" in body["frames"]
    assert LEAK not in line


def test_frames_are_capped_and_never_hold_the_message() -> None:
    def deep(left: int) -> None:
        if left:
            deep(left - 1)
            return
        raise RuntimeError(LEAK)

    try:
        deep(diagnostics.FRAME_LIMIT + 5)
    except RuntimeError as exc:
        frames = diagnostics.frames_of(exc)
    assert frames.count(" <- ") == diagnostics.FRAME_LIMIT and frames.endswith("...")
    assert LEAK not in frames


@async_test
async def test_a_domain_error_is_not_logged_as_internal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="spendgate.internal")
    # An untranslatable request is expected: 400, nothing to trace.
    monkeypatch.setattr(OpenAIProvider, "translate", domain)
    async with open_stack(tmp_path) as s:
        refused = await s.chat()
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid_request"
    assert internal_events(caplog) == []


@async_test
async def test_an_upstream_error_is_not_logged_as_internal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="spendgate.internal")
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(status=500))
        answered = await s.chat()
    assert answered.status_code == 502
    assert answered.json()["error"]["code"] == "upstream_unavailable"
    assert internal_events(caplog) == []