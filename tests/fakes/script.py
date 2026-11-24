"""What a test tells the fake upstream to do for one request."""

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class FakeUsage:
    """Provider-neutral usage numbers; each shape renders them in its own field names."""

    input_tokens: int = 10
    output_tokens: int = 5
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    audio_seconds: Decimal = Decimal(0)
    characters: int = 0


DEFAULT_USAGE = FakeUsage()
# Far beyond any real call: feeds the usage sanity flag (C-22). Booked in full, never clamped.
ABSURD_USAGE = FakeUsage(input_tokens=10**12, output_tokens=10**12)


@dataclass(frozen=True, slots=True)
class Script:
    """One request's behaviour. The default is a healthy 200 with `DEFAULT_USAGE`."""

    usage: FakeUsage = DEFAULT_USAGE
    status: int = 200  # >= 400 answers with the shape's error body
    retry_after: str | None = None  # Retry-After header value on the answer
    latency_ms: int = 0  # time to first byte, via the injected delay hook (no real sleep)
    frame_latency_ms: int = 0  # gap before each stream frame
    final_usage: bool = True  # False: a stream (or a 200) that carries no usage at all
    deltas: tuple[str, ...] = ("Hel", "lo")  # text pieces of the streamed reply
    cut_after_frames: int | None = None  # mid-stream cut: N frames, then the connection drops
    disconnect_before_body: bool = False  # connection closed with no response at all
    refuse_connection: bool = False  # request never sent (raised by the transport, not logged)
    hang: bool = False  # request is received and never answered (client cancel, timeout)
    oversize_event_bytes: int | None = None  # one stream event with this many bytes of text
    extra_events: int = 0  # this many additional empty-delta events (too many events)
    headers: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # extra response headers
    body: bytes | None = None  # a 200 answered with these JSON bytes in place of the shape's reply

    def stream_deltas(self) -> tuple[str, ...]:
        """Text pieces to stream, with the oversize piece and the extra empty events added."""
        pieces = list(self.deltas)
        if self.oversize_event_bytes is not None:
            pieces.insert(1, "x" * self.oversize_event_bytes)
        return tuple(pieces) + ("",) * self.extra_events
