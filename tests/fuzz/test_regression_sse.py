"""SSE regression found by the fuzz target (see test_regression_allowlists.py)."""

from spendgate.providers.port import SseEvent, SseLimits
from spendgate.providers.sse import SseParser


def test_fz04_line_at_the_limit_parses_whatever_the_chunking() -> None:
    limits = SseLimits(max_line_bytes=9)
    wire = b"data: abc\r\n\r\n"
    whole = SseParser(limits)
    assert whole.feed(wire) == [SseEvent(None, "abc")]
    split = SseParser(limits)
    events = split.feed(b"data: abc\r")  # chunk ends between CR and LF
    events += split.feed(b"\n\r\n")
    assert events == [SseEvent(None, "abc")]
