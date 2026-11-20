"""Test doubles. `FakeUpstream` plays any provider; see upstream.py."""

from tests.fakes.script import ABSURD_USAGE, DEFAULT_USAGE, FakeUsage, Script
from tests.fakes.upstream import (
    BASE_URL,
    FakeUpstream,
    FaultTransport,
    RecordedRequest,
    RefusingTransport,
)

__all__ = [
    "ABSURD_USAGE",
    "BASE_URL",
    "DEFAULT_USAGE",
    "FakeUpstream",
    "FakeUsage",
    "FaultTransport",
    "RecordedRequest",
    "RefusingTransport",
    "Script",
]
