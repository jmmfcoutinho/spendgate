"""The shared SSE codec: incremental parsing, limits, serialisation round trip."""

import pytest

from spendgate.core.cost import Usage
from spendgate.providers.port import SseEvent, SseLimits, StreamBroken, StreamLimitExceeded
from spendgate.providers.sse import SseParser, SseRelay, StreamMeter, asked_for_usage, serialise


def parse(raw: bytes) -> list[SseEvent]:
    parser = SseParser()
    return [*parser.feed(raw), *parser.close()]


def test_events_round_trip_through_serialise_and_parse() -> None:
    events = [
        SseEvent(None, "[DONE]"),
        SseEvent("message_start", '{"a":1}'),
        SseEvent(None, "a\nb"),
    ]
    assert parse(b"".join(serialise(e) for e in events)) == events


def test_every_byte_split_gives_the_same_events() -> None:
    raw = b"event: x\r\ndata: one\r\n\r\n: keep-alive\r\ndata: two\ndata: lines\n\ndata:three\r\r"
    expected = parse(raw)
    assert expected == [
        SseEvent("x", "one"),
        SseEvent(None, "two\nlines"),
        SseEvent(None, "three"),
    ]
    for size in (1, 2, 3, 5):
        parser = SseParser()
        got = [e for i in range(0, len(raw), size) for e in parser.feed(raw[i : i + size])]
        assert [*got, *parser.close()] == expected


def test_comments_other_fields_and_data_less_events_are_dropped() -> None:
    assert parse(b": hi\nid: 4\nretry: 5\n\nevent: only-a-name\n\n") == []


@pytest.mark.parametrize(
    ("limits", "raw"),
    [
        (SseLimits(max_line_bytes=8), b"data: 123456789\n\n"),
        (SseLimits(max_line_bytes=8), b"data: 1234567"),  # a partial line already too long
        (SseLimits(max_event_bytes=20), b"data: 1234567\ndata: 1234567\ndata: 1234567\n\n"),
        (SseLimits(max_events=1), b"data: 1\n\ndata: 2\n\n"),
        (SseLimits(max_stream_bytes=10), b"data: 12345\n\ndata: 6\n\n"),
    ],
    ids=["line", "partial-line", "event", "events", "stream"],
)
def test_each_limit_raises_stream_limit_exceeded(limits: SseLimits, raw: bytes) -> None:
    with pytest.raises(StreamLimitExceeded):
        SseParser(limits).feed(raw)


def test_a_half_event_or_bad_utf8_is_a_broken_stream() -> None:
    parser = SseParser()
    parser.feed(b"data: {")
    with pytest.raises(StreamBroken):
        parser.close()
    with pytest.raises(StreamBroken):
        SseParser().feed(b"data: \xff\n\n")


def test_the_meter_counts_bytes_and_events() -> None:
    meter = StreamMeter(SseLimits(max_events=1, max_stream_bytes=4))
    meter.add_bytes(4)
    meter.add_event()
    with pytest.raises(StreamLimitExceeded):
        meter.add_bytes(1)
    with pytest.raises(StreamLimitExceeded):
        meter.add_event()


def test_asked_for_usage_reads_only_an_explicit_true() -> None:
    assert asked_for_usage({"stream_options": {"include_usage": True}})
    assert not asked_for_usage({"stream_options": {"include_usage": False}})
    assert not asked_for_usage({})


class _Last:
    def __init__(self) -> None:
        self.seen: list[SseEvent] = []

    def feed(self, event: SseEvent) -> None:
        self.seen.append(event)

    def result(self) -> Usage | None:
        return Usage(input_tokens=len(self.seen))


def test_the_relay_reads_every_event_but_hides_what_it_is_told_to() -> None:
    reader = _Last()
    relay = SseRelay(reader, SseLimits(), lambda e: e.data == "secret")
    out = relay.feed(b"data: a\n\ndata: secret\n\n") + relay.close()
    assert out == b"data: a\n\n"
    assert len(reader.seen) == 2 and relay.usage() == Usage(input_tokens=2)
