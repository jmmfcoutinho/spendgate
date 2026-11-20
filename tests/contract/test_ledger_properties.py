"""Stateful property test: random operations keep the invariant and match the pure model.

I1: spent + held <= effective cap + overrun + excess, checked on the budgets an operation
normalised. 500 examples per adapter, 25 steps each, against `ledger_model.LedgerModel`.
"""

import os
import tempfile
from pathlib import Path
from typing import ClassVar

import pytest
from hypothesis import HealthCheck, event, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from spendgate.core.budgets import PeriodState, invariant_holds
from spendgate.ledger.port import Held
from tests.contract.ledger_model import LedgerModel
from tests.contract.rig import (
    LEASE_MS,
    POSTGRES_HOOK_READY,
    SECOND,
    T0,
    E,
    Rig,
    World,
    workdir_location,
)

QUARTER = E("0.25")
NAMES = ("org", "user")
PROFILE = settings(
    max_examples=500,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=list(HealthCheck),
    database=None,  # no example database on disk: the suite stays hermetic
)


class LedgerMachine(RuleBasedStateMachine):
    kind: ClassVar[str] = "sqlite"

    def __init__(self) -> None:
        super().__init__()
        self.dir = tempfile.TemporaryDirectory(prefix="spendgate-prop-")
        counter = iter(range(10**9))
        self.rig = Rig(
            self.kind,
            workdir_location(self.kind, Path(self.dir.name)),
            lambda: f"id{next(counter):09d}",
            synchronous="OFF",  # durability is SQLite's; this test checks the logic
        )
        self.w: World = self.rig.world(
            "4", org_cap="6"
        )  # small caps: grants, cuts and overruns bite quickly
        self.ids = {"user": self.w.budget, "org": self.w.extra[0]}
        self.model = LedgerModel({"user": E("4"), "org": E("6")})
        self.now = T0
        self.count = 0
        self.known: list[str] = []  # every request id ever admitted, any state
        # Start with short grants so spending above the plain cap, and grants expiring while
        # over it, are common rather than rare.
        for name in NAMES:
            self.grant(name, 3, 40)

    def teardown(self) -> None:
        self.rig.close()
        self.dir.cleanup()

    def compare(self, normalised: tuple[str, ...] = ()) -> None:
        """Ledger totals equal the model; I1 holds on the budgets the step normalised."""
        for name in NAMES:
            t = self.rig.totals(self.w, self.now, budget=self.ids[name])
            figures = (t.spent_nano, t.held_nano, t.overrun_nano, t.excess_nano, t.calls)
            flag = t.review_required_at is not None
            assert (*figures, t.effective_cap_nano, flag) == self.model.snapshot(name, self.now), (
                name
            )
            if name in normalised:
                assert invariant_holds(PeriodState(*figures[:4]), t.effective_cap_nano), (name, t)

    @invariant()
    def totals_match_the_model(self) -> None:
        self.compare()

    @rule(ms=st.integers(1, 40_000))
    def tick(self, ms: int) -> None:
        self.now += ms

    @rule(units=st.integers(1, 8), owner=st.sampled_from(["A", "B"]))
    def hold(self, units: int, owner: str) -> None:
        self.do_hold(units, owner)

    @rule(units=st.integers(1, 8), owner=st.sampled_from(["A", "B"]))
    def hold_more(self, units: int, owner: str) -> None:  # holds are the busiest operation
        self.do_hold(units, owner)

    def do_hold(self, units: int, owner: str) -> None:
        rid = f"r{self.count}"
        self.count += 1
        worst = units * QUARTER
        got = self.rig.hold_nano(self.w, rid, worst, self.now, instance=owner)
        before = [self.model.budgets[n].excess for n in NAMES]
        assert isinstance(got, Held) == self.model.hold(rid, worst, self.now, LEASE_MS, owner)
        grew = [self.model.budgets[n].excess for n in NAMES] != before
        event(f"hold admitted={isinstance(got, Held)} excess_grew={grew}")
        if isinstance(got, Held):
            self.known.append(rid)
        self.compare(NAMES)  # admission normalises the whole chain, admitted or not

    @precondition(lambda self: bool(self.known))
    @rule(data=st.data(), units=st.integers(0, 16))
    def settle(self, data: st.DataObject, units: int) -> None:
        rid = data.draw(st.sampled_from(self.known))
        charge = units * QUARTER  # up to 4.00: sometimes above the hold, so overruns happen
        self.rig.settle_nano(rid, charge, self.now)
        self.model.settle(rid, charge, self.now)

    @precondition(lambda self: bool(self.known))
    @rule(data=st.data())
    def release(self, data: st.DataObject) -> None:
        rid = data.draw(st.sampled_from(self.known))
        self.rig.release(rid, self.now)
        self.model.release(rid)

    @rule()
    def sweep(self) -> None:
        got = self.rig.sweep(self.now)
        assert sorted(got) == self.model.sweep(self.now)
        self.compare(NAMES)

    @precondition(lambda self: bool(self.known))
    @rule(owner=st.sampled_from(["A", "B"]), data=st.data())
    def renew(self, owner: str, data: st.DataObject) -> None:
        ids = set(data.draw(st.lists(st.sampled_from(self.known), max_size=6)))
        got = self.rig.run(self.rig.ledger.renew_all(owner, ids, self.now, LEASE_MS))
        assert list(got) == self.model.renew(owner, ids, self.now, LEASE_MS)

    @rule(name=st.sampled_from(NAMES), cap=st.integers(0, 8))
    def set_cap(self, name: str, cap: int) -> None:
        self.rig.run(
            self.rig.ledger.update_budget(
                self.ids[name].id, actor="prop", now=self.now, cap_nano=E(str(cap))
            )
        )
        self.model.set_cap(name, E(str(cap)), self.now)
        self.compare((name,))

    @rule(name=st.sampled_from(NAMES), amount=st.integers(1, 6), seconds=st.integers(1, 120))
    def grant(self, name: str, amount: int, seconds: int) -> None:
        end = self.now + seconds * SECOND
        self.rig.run(
            self.rig.ledger.create_grant(
                self.ids[name].id, amount_nano=E(str(amount)), valid_from=self.now,
                valid_until=end, actor="prop", reason="prop", now=self.now,
            )
        )  # fmt: skip
        self.model.add_grant(name, E(str(amount)), self.now, end, self.now)
        self.compare((name,))

    @rule(name=st.sampled_from(NAMES))
    def ack(self, name: str) -> None:
        self.rig.run(
            self.rig.ledger.ack_overrun(self.ids[name].id, actor="prop", reason="p", now=self.now)
        )
        self.model.ack(name)


class PostgresLedgerMachine(LedgerMachine):
    kind: ClassVar[str] = "postgres"


TestSqliteLedgerStateful = LedgerMachine.TestCase
TestSqliteLedgerStateful.settings = PROFILE

# Postgres commits fsync on every step, so CI runs fewer examples (SPENDGATE_PG_STATEFUL_EXAMPLES);
# a manual run keeps the full 500. Bound to one module name only: pytest collects every
# TestCase subclass by name, and a second alias would run the whole machine twice.
TestPostgresLedgerStateful = pytest.mark.postgres(
    pytest.mark.usefixtures("postgres_url")(
        pytest.mark.skipif(not POSTGRES_HOOK_READY, reason="Postgres hook not filled in (3.2)")(
            PostgresLedgerMachine.TestCase
        )
    )
)
TestPostgresLedgerStateful.settings = settings(
    PROFILE, max_examples=int(os.environ.get("SPENDGATE_PG_STATEFUL_EXAMPLES", "500"))
)


def test_the_model_itself_obeys_the_invariant_on_a_hand_run() -> None:
    """Guards the model: scenario 10 by hand, so a wrong model cannot hide a wrong ledger."""
    model = LedgerModel({"b": E("10")})
    assert model.hold("a", E("9"), 0, LEASE_MS, "A")
    model.settle("a", E("9"), 0)
    model.add_grant("b", E("5"), 0, 1000, 0)
    assert model.hold("c", E("5"), 500, LEASE_MS, "A")
    assert not model.hold("d", E("1"), 1500, LEASE_MS, "A")
    assert model.snapshot("b", 1500) == (E("9"), E("5"), 0, E("4"), 1, E("10"), False)
