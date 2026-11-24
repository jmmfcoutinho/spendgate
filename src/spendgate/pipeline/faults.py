"""Redacted traces of unexpected exceptions that a catch-all turned into `internal_error`.

A catch-all keeps money handling correct when an adapter or the pipeline has a bug, but without a
record nobody can find the bug. The record keeps only what cannot carry request data: the
exception class and where each frame was (module, function, line). The message, the locals and the
source lines are left out on purpose: an exception message can quote a body, a header or a secret.
"""

import traceback
from dataclasses import dataclass
from types import FrameType

MAX_FRAMES = 20  # the innermost ones: where the bug is, and a bounded log line
_UNKNOWN = "?"


@dataclass(frozen=True, slots=True)
class Fault:
    """Where a catch-all caught what, without anything the exception said."""

    where: str  # the catch-all's tag, e.g. `routing.response`
    error_class: str
    frames: tuple[str, ...]  # `module:function:line`, outermost first

    @property
    def frames_text(self) -> str:
        """The frames as one scalar log field (the log guard keeps scalars only)."""
        return " > ".join(self.frames)


@dataclass(frozen=True, slots=True)
class InternalEvent:
    """A catch-all answered `internal_error` (or refused) because of an unexpected exception."""

    request_id: str
    fault: Fault
    settled: bool = False  # the call's settle transition (a charge) was booked before the answer


def fault_of(where: str, exc: BaseException) -> Fault:
    """The redacted trace of `exc`. Never reads `str(exc)`, frame locals or source lines.

    Frames come from `traceback.walk_tb` rather than `extract_tb`: the frame's module name is
    exact, and a file path (which can name the machine's user) is never needed.
    """
    steps = list(traceback.walk_tb(exc.__traceback__))[-MAX_FRAMES:]
    named = tuple(f"{_module(f)}:{f.f_code.co_qualname}:{line}" for f, line in steps)
    return Fault(where, type(exc).__name__, named)


def _module(frame: FrameType) -> str:
    name = frame.f_globals.get("__name__")
    return name if isinstance(name, str) else _UNKNOWN
