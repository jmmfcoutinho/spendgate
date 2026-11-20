"""A private event loop on a background thread, for the synchronous transport.

Why a thread: the pipeline is async, a plain `httpx.Client` is not, and `asyncio.run` fails inside
Jupyter (a loop is already running there). A loop that this module owns works everywhere; the
caller's thread just waits for the result.
"""

import asyncio
import threading
from collections.abc import Coroutine


class LoopThread:
    """Runs coroutines to completion on its own loop; `close` stops the thread."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._main, name="spendgate-local", daemon=True)
        self._thread.start()

    def _main(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run[T](self, coro: Coroutine[object, object, T]) -> T:
        """Block until `coro` finishes on the loop thread; its exception is re-raised here."""
        if threading.current_thread() is self._thread:
            coro.close()
            raise RuntimeError("LoopThread.run called from its own thread (it would deadlock)")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result()
        except BaseException:  # Ctrl-C while waiting: cancel so the pipeline settles the hold
            future.cancel()
            raise

    def close(self) -> None:
        """Stop the loop and join the thread; safe to call twice."""
        if self._loop.is_closed():
            return
        # Scheduled even if the thread has not reached run_forever yet: the first iteration stops.
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()
