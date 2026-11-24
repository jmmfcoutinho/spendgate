"""The redacted trace a catch-all leaves: class and frames, never the message or a file path."""

from spendgate.gateway.logs import redact
from spendgate.pipeline.faults import MAX_FRAMES, fault_of

SECRET = "sk-fake-" + "1" * 24  # pragma: allowlist secret


def deep(n: int) -> None:
    if n == 0:
        raise IndexError(f"list index out of range {SECRET}")
    deep(n - 1)


def caught(n: int) -> BaseException:
    try:
        deep(n)
    except IndexError as exc:
        return exc
    raise AssertionError("deep() always raises")


def test_a_fault_names_class_and_frames_but_not_the_message() -> None:
    fault = fault_of("test.where", caught(1))
    assert (fault.where, fault.error_class) == ("test.where", "IndexError")
    assert [f.rsplit(":", 1)[0] for f in fault.frames] == [
        f"{__name__}:caught", f"{__name__}:deep", f"{__name__}:deep",
    ]  # fmt: skip
    assert all(f.rsplit(":", 1)[1].isdigit() for f in fault.frames)
    assert SECRET not in repr(fault) and "out of range" not in repr(fault)
    assert "/" not in fault.frames_text  # module names, never file paths


def test_a_fault_keeps_only_the_innermost_frames() -> None:
    fault = fault_of("test.where", caught(MAX_FRAMES * 2))
    assert len(fault.frames) == MAX_FRAMES
    assert all(f.startswith(f"{__name__}:deep:") for f in fault.frames)


def test_the_frames_reach_the_log_as_one_scalar_field() -> None:
    fault = fault_of("test.where", caught(0))
    kept = redact({"frames": fault.frames_text, "where": fault.where, "error_class": "X"})
    assert kept == {"frames": fault.frames_text, "where": "test.where", "error_class": "X"}
    assert redact({"frames": list(fault.frames)}) == {}  # a list would be dropped by the guard
