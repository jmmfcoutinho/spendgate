"""SIGHUP in `serve`: read `file:` secrets again and hand them to the running server.

Rotating a provider key or the database password is then a file write plus `kill -HUP`, with no
restart and no drain. What is reloaded is in `config.reload`; this module applies it and logs the
config paths that were read, changed or failed (never a value). Calls already in flight finish
with the credential they started with; the next call uses the new one.

The files are read in a daemon thread of their own: a slow or hung mount must not stall the event
loop and every request on it, nor hold shutdown (`asyncio.run` waits for the default executor's
threads; a daemon thread is left behind). Only applying the result runs on the loop.
"""

import asyncio
import contextlib
import logging
import signal
import threading
from asyncio import AbstractEventLoop
from collections.abc import Callable, Iterator
from dataclasses import replace

from spendgate.config import Reread, Settings, reread_file_secrets
from spendgate.gateway.logs import log_event
from spendgate.pipeline.pipeline import Pipeline

DSN_PATH = "ledger.backend.dsn"
type Reader = Callable[[Settings], Reread]  # blocking file reads


class SecretReload:
    """The SIGHUP handler: schedules one reload task; signals during a reload coalesce."""

    def __init__(
        self,
        settings: Settings,
        pipeline: Pipeline,
        logger: logging.Logger,
        rotate_dsn: Callable[[str], bool] | None = None,
        *,
        read: Reader = reread_file_secrets,
    ) -> None:
        self._settings = settings
        self._pipeline, self._logger, self._rotate_dsn = pipeline, logger, rotate_dsn
        self._read = read
        self._task: asyncio.Task[None] | None = None  # kept: the loop holds tasks only weakly
        self._again = False  # a SIGHUP arrived while a reload ran: read once more after it
        self._closed = False

    @property
    def settings(self) -> Settings:
        """The settings in use, with the secrets of the last successful reload."""
        return self._settings

    def __call__(self) -> None:
        """Runs on the loop (`add_signal_handler`); returns at once, never raises."""
        if self._closed:
            return
        if self._task is not None and not self._task.done():
            self._again = True  # the file may have changed after this reload read it
            return
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def wait(self) -> None:
        """Until the reload in progress, and any it coalesced, has finished."""
        if self._task is not None:
            await asyncio.shield(self._task)

    def close(self) -> None:
        """At shutdown: no more reloads, and a read still under way is abandoned, not awaited."""
        self._closed = True
        if self._task is not None:
            self._task.cancel()

    async def _run(self) -> None:
        while True:
            self._again = False
            try:
                self._apply(await _in_daemon_thread(self._read, self._settings))
            except Exception as exc:  # the server keeps the secrets it has
                log_event(
                    self._logger, "secret_reload_failed", logging.ERROR,
                    error_class=type(exc).__name__,
                )  # fmt: skip
            if not self._again:
                return

    def _apply(self, result: Reread) -> None:
        failed, changed, new = list(result.failed), list(result.changed), result.settings
        # Build the new targets before touching the ledger: if this raises, nothing has changed.
        commit = self._pipeline.reload_credentials(new.routes)
        if DSN_PATH in changed and (problem := self._rotate(new)) is not None:
            changed.remove(DSN_PATH)
            failed.append((DSN_PATH, problem))
            new = replace(new, ledger_dsn=self._settings.ledger_dsn)
        commit(new)
        self._settings = new
        for where, reason in failed:
            log_event(
                self._logger, "secret_reload_failed", logging.WARNING, where=where, reason=reason
            )
        log_event(
            self._logger, "secrets_reloaded",
            reloaded=",".join(result.reloaded), changed=",".join(changed),
        )  # fmt: skip

    def _rotate(self, new: Settings) -> str | None:
        """Hand the new DSN to the ledger; why it was refused, or None."""
        if self._rotate_dsn is None or new.ledger_dsn is None:
            return None  # nothing holds the DSN open (a migrate run): the new value is kept
        try:
            self._rotate_dsn(new.ledger_dsn.reveal())
        except ValueError as exc:  # fixed texts from `rotate_dsn`, never the DSN itself
            return f"{exc}; the old DSN stays in use"
        return None


def _in_daemon_thread(read: Reader, settings: Settings) -> "asyncio.Future[Reread]":
    """`read(settings)` in a new daemon thread; its result or error lands in the future."""
    loop = asyncio.get_running_loop()
    done: asyncio.Future[Reread] = loop.create_future()

    def post[T](setter: Callable[[T], object], value: T) -> None:
        def run() -> None:
            if not done.cancelled():  # closed or shut down while the files were read
                setter(value)

        with contextlib.suppress(RuntimeError):  # the loop has closed: nobody is waiting
            loop.call_soon_threadsafe(run)

    def work() -> None:
        try:
            result = read(settings)
        except BaseException as exc:  # handed to the loop, which logs it; any error, or the
            # task would wait forever and no reload would run again. One that is not an
            # Exception is wrapped: raised in the task, it would stop the loop itself.
            error = exc if isinstance(exc, Exception) else RuntimeError(type(exc).__name__)
            post(done.set_exception, error)
        else:
            post(done.set_result, result)

    threading.Thread(target=work, name="spendgate-secret-reload", daemon=True).start()
    return done


@contextlib.contextmanager
def on_sighup(loop: AbstractEventLoop, reload: SecretReload) -> Iterator[None]:
    """`reload` runs on SIGHUP inside the block; on leaving it, reloads stop and the task goes."""
    install(loop, reload)
    try:
        yield
    finally:
        # The handler stays: removing it restores the default action, and a SIGHUP while the
        # ledger closes would then end the process mid-close. A closed reload ignores signals,
        # and closing the loop removes the handler.
        reload.close()


def install(loop: AbstractEventLoop, handler: Callable[[], None]) -> bool:
    """Run `handler` on SIGHUP; False where the platform has no SIGHUP (Windows)."""
    hup = getattr(signal, "SIGHUP", None)
    if hup is None:  # pragma: no cover - POSIX in CI
        return False
    loop.add_signal_handler(hup, handler)
    return True
