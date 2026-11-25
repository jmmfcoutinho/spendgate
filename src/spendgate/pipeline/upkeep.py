"""Background upkeep of one pipeline: lease renewal, the stream watch (protocol 2.8) and the loop.

`heartbeat` is the one supervised loop both the gateway and library mode run. A step that raises
is logged and the loop goes on: a heartbeat that dies quietly would let every owned lease run out,
so the sweeper would charge calls still in flight in full and open streams would stop.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence

from spendgate.ledger.port import MoneyLedger
from spendgate.pipeline.closing import Closer
from spendgate.pipeline.leases import Leases
from spendgate.pipeline.model import Clock, NoopObserver, Observer, report_ledger

_log = logging.getLogger("spendgate.pipeline")
type Step = tuple[str, Callable[[], Awaitable[object]]]


async def heartbeat(
    interval: float,
    steps: Sequence[Step],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_failure: Callable[[str], None] | None = None,
) -> None:
    """Run `steps` in order every `interval` seconds until cancelled; one failing never stops it.

    `on_failure` gets the failed step's name (a metric); if it raises, that is ignored.
    """
    while True:
        await sleep(interval)
        for name, step in steps:
            try:
                await step()
            except asyncio.CancelledError:
                if _stopping():
                    raise  # the loop itself was cancelled: stop
                _failed(name, "CancelledError", on_failure)  # a step's own inner await
            except Exception as exc:
                _failed(name, type(exc).__name__, on_failure)


def _stopping() -> bool:
    task = asyncio.current_task()
    return task is None or task.cancelling() > 0


def _failed(step: str, reason: str, on_failure: Callable[[str], None] | None) -> None:
    """The next beat retries. Only the type is logged: messages may hold URLs or ids."""
    _log.error("heartbeat step failed step=%s reason=%s", step, reason)
    if on_failure is not None:
        with contextlib.suppress(Exception):
            on_failure(step)


class Upkeep:
    """What the heartbeat does, over the ledger, leases and closer the pipeline owns."""

    def __init__(  # noqa: PLR0913, PLR0917 - the pipeline's own collaborators, passed through
        self,
        ledger: MoneyLedger,
        closer: Closer,
        leases: Leases,
        owned: set[str],
        now: Clock,
        instance: str,
        lease_ms: int,
        observer: Observer | None = None,
    ) -> None:
        self._ledger, self._closer, self._leases, self._owned = ledger, closer, leases, owned
        self._now, self._instance, self._lease_ms = now, instance, lease_ms
        self._observer = observer or NoopObserver()

    async def watch_streams(self) -> None:
        """Stop open streams a kill switch or their session's time limit now forbids (2.8)."""
        if not self._leases.watching():
            return
        try:
            switches = await self._ledger.active_kill_switches()
        except Exception:  # the lease logic already stops streams while the ledger is down
            switches = ()
        self._leases.stop_forbidden(switches, self._now())

    async def renew_leases(self) -> None:
        """One heartbeat. A stream whose lease was not renewed is aborted and settled.

        Renews only the holds this pipeline still owns (calls in flight and open streams). A hold
        whose close the ledger refused is retried here and no longer renewed, so it reaches the
        sweeper and is charged in full (CR-02).
        """
        await self._closer.retry_unsettled()
        now = self._now()
        self._leases.expire(now)  # an overdue stream is settled even if its client stopped reading
        watched = self._leases.watched()
        owned = (self._owned | watched) - self._closer.unsettled() - self._leases.stopping()
        try:
            renewed = await self._ledger.renew_all(self._instance, owned, now, self._lease_ms)
        except Exception:  # nobody knows the leases are alive: streams stop, the next beat retries
            report_ledger(self._observer, "renew", now, self._now(), ok=False)
            self._leases.failed(watched)
            return
        if owned:  # an empty renewal never reaches the ledger: no latency to report
            report_ledger(self._observer, "renew", now, self._now(), ok=True)
        self._leases.renewed(now, watched, frozenset(renewed))
