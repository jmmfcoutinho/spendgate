"""SIGHUP in `serve`: read `file:` secrets again and hand them to the running server.

Rotating a provider key or the database password is then a file write plus `kill -HUP`, with no
restart and no drain. What is reloaded is in `config.reload`; this module applies it and logs the
config paths that were read, changed or failed (never a value). Calls already in flight finish
with the credential they started with; the next call uses the new one.

The files are read in a worker thread: a slow or hung mount must not stall the event loop and
every request on it. Only applying the result runs on the loop.
"""

import asyncio
import logging
import signal
from asyncio import AbstractEventLoop
from collections.abc import Callable
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

    @property
    def settings(self) -> Settings:
        """The settings in use, with the secrets of the last successful reload."""
        return self._settings

    def __call__(self) -> None:
        """Runs on the loop (`add_signal_handler`); returns at once, never raises."""
        if self._task is not None and not self._task.done():
            self._again = True  # the file may have changed after this reload read it
            return
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def wait(self) -> None:
        """Until the reload in progress, and any it coalesced, has finished."""
        if self._task is not None:
            await asyncio.shield(self._task)

    async def _run(self) -> None:
        while True:
            self._again = False
            try:
                self._apply(await asyncio.to_thread(self._read, self._settings))
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


def install(loop: AbstractEventLoop, handler: Callable[[], None]) -> bool:
    """Run `handler` on SIGHUP; False where the platform has no SIGHUP (Windows)."""
    hup = getattr(signal, "SIGHUP", None)
    if hup is None:  # pragma: no cover - POSIX in CI
        return False
    loop.add_signal_handler(hup, handler)
    return True
