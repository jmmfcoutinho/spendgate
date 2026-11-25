"""`Leases`: which streams a heartbeat aborts, without a ledger."""

from spendgate.core.failures import StopReason
from spendgate.pipeline.leases import Leases

LEASE, BEAT = 60_000, 20_000


class FakeStream:
    def __init__(self) -> None:
        self.reasons: list[StopReason] = []

    def abort(self, reason: StopReason) -> None:
        self.reasons.append(reason)

    async def aclose(self) -> None:
        pass


def test_a_renewal_extends_the_lease_of_every_stream_it_returned() -> None:
    leases, a = Leases(LEASE, BEAT), FakeStream()
    leases.register("a", 0, a)
    assert not leases.near_expiry("a", 39_999) and leases.near_expiry("a", 40_001)
    leases.renewed(30_000, leases.watched(), frozenset({"a"}))
    assert not leases.near_expiry("a", 69_999) and leases.near_expiry("a", 70_001)
    assert a.reasons == []


def test_a_stream_missing_from_the_renewal_was_expired_by_a_sweeper() -> None:
    leases, a, b = Leases(LEASE, BEAT), FakeStream(), FakeStream()
    leases.register("a", 0, a)
    leases.register("b", 0, b)
    leases.renewed(1, leases.watched(), frozenset({"a"}))
    assert (a.reasons, b.reasons) == ([], ["lease_lost"])


def test_a_stream_opened_while_the_renewal_ran_is_not_judged_by_it() -> None:
    leases, early, late = Leases(LEASE, BEAT), FakeStream(), FakeStream()
    leases.register("early", 0, early)
    watched = leases.watched()  # taken before the renewal starts
    leases.register("late", 5, late)  # its hold may have committed after the statement ran
    leases.renewed(10, watched, frozenset({"early"}))
    assert late.reasons == []


def test_a_failed_renewal_aborts_every_watched_stream_and_closed_ones_are_ignored() -> None:
    leases, a, b = Leases(LEASE, BEAT), FakeStream(), FakeStream()
    leases.register("a", 0, a)
    leases.register("b", 0, b)
    watched = leases.watched()
    leases.unregister("b")
    leases.failed(watched)
    assert (a.reasons, b.reasons) == (["renewal_failed"], [])
    assert leases.open() == [a]
