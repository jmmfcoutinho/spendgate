"""A credential refresh that blocks its worker thread until the test opens the gate.

"The event loop kept running" is then observed directly, without real sleeps. A refresh made on
the loop itself would hold the loop until the timeout and then raise: the test fails, never hangs.
"""

import threading

GATE_TIMEOUT_S = 5  # only reached when a refresh blocks the loop (the bug): fail, never hang


class Gate:
    """A credential refresh that blocks its thread until the test opens the gate."""

    def __init__(self, *, fail: bool = False) -> None:
        self.entered = threading.Event()
        self.opened = threading.Event()
        self.threads: list[int] = []
        self.fail = fail
        self.timed_out = False  # the gate was never opened: the loop was blocked meanwhile
        self._lock = threading.Lock()

    @property
    def calls(self) -> int:
        return len(self.threads)

    def refresh(self) -> None:
        with self._lock:
            self.threads.append(threading.get_ident())
        self.entered.set()
        if not self.opened.wait(GATE_TIMEOUT_S):
            self.timed_out = True
            raise TimeoutError("the refresh blocked the event loop")
        if self.fail:
            raise RuntimeError("the identity provider refused")  # an operator fault
