"""SIGHUP in `serve`: read `file:` secrets again and hand them to the running server.

Rotating a provider key or the database password is then a file write plus `kill -HUP`, with no
restart and no drain. What is reloaded is in `config.reload`; this module applies it and logs the
config paths that were read, changed or failed (never a value). Calls already in flight finish
with the credential they started with; the next call uses the new one.
"""

import logging
import signal
from asyncio import AbstractEventLoop
from collections.abc import Callable
from dataclasses import replace

from spendgate.config import Settings, reread_file_secrets
from spendgate.gateway.logs import log_event
from spendgate.pipeline.pipeline import Pipeline

DSN_PATH = "ledger.backend.dsn"


class SecretReload:
    """The SIGHUP handler: synchronous, small file reads only, safe to call on the loop."""

    def __init__(
        self,
        settings: Settings,
        pipeline: Pipeline,
        logger: logging.Logger,
        rotate_dsn: Callable[[str], bool] | None = None,
    ) -> None:
        self._settings = settings
        self._pipeline, self._logger, self._rotate_dsn = pipeline, logger, rotate_dsn

    @property
    def settings(self) -> Settings:
        """The settings in use, with the secrets of the last successful reload."""
        return self._settings

    def __call__(self) -> None:
        try:
            self._reload()
        except Exception as exc:  # a signal handler must not raise: the server keeps its secrets
            log_event(
                self._logger, "secret_reload_failed", logging.ERROR,
                error_class=type(exc).__name__,
            )  # fmt: skip

    def _reload(self) -> None:
        result = reread_file_secrets(self._settings)
        failed = list(result.failed)
        changed = list(result.changed)
        new = result.settings
        if DSN_PATH in changed and not self._rotate(new):
            changed.remove(DSN_PATH)
            failed.append((DSN_PATH, "the DSN changed more than its password; restart to apply"))
            new = replace(new, ledger_dsn=self._settings.ledger_dsn)
        self._pipeline.reload_credentials(new)
        self._settings = new
        for where, reason in failed:
            log_event(
                self._logger, "secret_reload_failed", logging.WARNING, where=where, reason=reason
            )
        log_event(
            self._logger, "secrets_reloaded",
            reloaded=",".join(result.reloaded), changed=",".join(changed),
        )  # fmt: skip

    def _rotate(self, new: Settings) -> bool:
        if self._rotate_dsn is None or new.ledger_dsn is None:
            return True  # nothing holds the DSN open (a migrate run): the new value is kept
        try:
            self._rotate_dsn(new.ledger_dsn.reveal())
        except ValueError:
            return False
        return True


def install(loop: AbstractEventLoop, handler: Callable[[], None]) -> bool:
    """Run `handler` on SIGHUP; False where the platform has no SIGHUP (Windows)."""
    hup = getattr(signal, "SIGHUP", None)
    if hup is None:  # pragma: no cover - POSIX in CI
        return False
    loop.add_signal_handler(hup, handler)
    return True
