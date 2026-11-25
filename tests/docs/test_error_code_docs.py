"""ADR-0022: every value the pipeline can write to `calls.error_code` is documented.

The values are collected from `CallErrorCode` itself (the type `AttemptRecord.error_code` takes),
so a new enum, or a new member of one, fails here until docs/data-model.md lists it.
"""

import re
from enum import StrEnum
from pathlib import Path
from typing import get_args

from spendgate.core.errors import ErrorCode
from spendgate.core.failures import AttemptError, CallErrorCode, StopReason
from spendgate.core.outcomes import Outcome
from spendgate.providers.bedrock_stream import EventStreamError, FrameTooLarge
from spendgate.providers.port import StreamBroken, StreamLimitExceeded
from tests.unit.test_failure_codes import HTTPX_CODES

REPO = Path(__file__).resolve().parents[2]
DATA_MODEL = (REPO / "docs" / "data-model.md").read_text()
PROTOCOL = (REPO / "docs" / "protocol.md").read_text()


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start : end if end != -1 else None]


SECTION = _section(DATA_MODEL, "## `calls.error_code` values")
TABLE = {m.group(1) for m in re.finditer(r"^\| `([a-z_]+)` \|", SECTION, re.MULTILINE)}
LEGACY = SECTION[SECTION.index("**Legacy values.**") :]


def written_enums() -> tuple[type[StrEnum], ...]:
    return get_args(CallErrorCode.__value__)


def test_the_written_values_come_from_exactly_these_enums() -> None:
    assert set(written_enums()) == {StopReason, AttemptError, ErrorCode, Outcome}


def test_every_stop_reason_and_attempt_error_has_a_row() -> None:
    ours = {m.value for m in (*StopReason, *AttemptError)}
    assert ours == TABLE  # no value undocumented, no row for a value that is never written


def test_provider_codes_and_interrupted_outcomes_are_documented_where_they_are_defined() -> None:
    codes = re.findall(r"^\| `([a-z_]+)` \| \d{3}", PROTOCOL, re.MULTILINE)
    assert set(codes) == {c.value for c in ErrorCode}  # protocol.md section 4.2
    check = re.search(r"outcome\s+TEXT\s+NOT NULL CHECK \(outcome IN \(([^)]*)\)\)", DATA_MODEL)
    assert check is not None
    assert set(re.findall(r"'([a-z0-9_]+)'", check.group(1))) == {o.value for o in Outcome}
    for interrupted in (Outcome.CANCELLED, Outcome.TIMEOUT, Outcome.INTERNAL_ERROR):
        assert f"`{interrupted.value}`" in SECTION


def test_the_old_exception_class_names_are_listed_as_legacy() -> None:
    catch_all = {AttemptError.TRANSPORT_ERROR, AttemptError.UNEXPECTED_ERROR}
    specific = {cls for cls, code in HTTPX_CODES.items() if code not in catch_all}
    streams = {StreamBroken, StreamLimitExceeded, EventStreamError, FrameTooLarge}
    names = {c.__name__ for c in specific | streams} | {"ResponseTooLarge"}
    assert {n for n in names if f"`{n}`" not in LEGACY} == set()
