"""A pure in-memory model of the ledger's money state, for the stateful property test.

It is written from ledger-state-machine.md, not from the SQL flows, and uses no core function:
two independent implementations that must agree after every random step. It models one chain of
named budgets (period `none`) that every hold passes through.
"""

from dataclasses import dataclass, field


@dataclass
class ModelBudget:
    cap: int
    strict: bool = True
    grants: list[tuple[int, int, int]] = field(default_factory=list)  # amount, from, until
    spent: int = 0
    held: int = 0
    overrun: int = 0
    excess: int = 0
    calls: int = 0
    review: bool = False

    def effective(self, now: int) -> int:
        return self.cap + sum(a for a, start, end in self.grants if start <= now < end)

    def needed_excess(self, now: int) -> int:
        return max(0, self.spent + self.held - self.effective(now) - self.overrun)


@dataclass
class ModelReservation:
    owner: str
    held: int
    lease: int
    state: str = "pending"
    late_settled: bool = False


Snapshot = tuple[int, int, int, int, int, int, bool]  # S, H, ovr, exc, calls, eff, review


class LedgerModel:
    def __init__(self, caps: dict[str, int]) -> None:
        self.budgets = {name: ModelBudget(cap) for name, cap in caps.items()}
        self.reservations: dict[str, ModelReservation] = {}

    def snapshot(self, name: str, now: int) -> Snapshot:
        b = self.budgets[name]
        return b.spent, b.held, b.overrun, b.excess, b.calls, b.effective(now), b.review

    # --- transitions -------------------------------------------------------------------

    def hold(self, rid: str, worst: int, now: int, lease_ms: int, owner: str) -> bool:
        admitted = True
        for b in self.budgets.values():
            b.excess = max(b.excess, b.needed_excess(now))  # recorded even when refusing (A1)
            if b.review or b.spent + b.held + worst > b.effective(now):
                admitted = False
        if admitted:
            for b in self.budgets.values():
                b.held += worst
            self.reservations[rid] = ModelReservation(owner, worst, now + lease_ms)
        return admitted

    def settle(self, rid: str, charge: int, now: int) -> None:
        res = self.reservations[rid]
        extra = max(0, charge - res.held)
        if res.state == "pending":
            res.state = "settled"
            for b in self.budgets.values():
                b.held -= res.held
                b.spent += charge
                b.overrun += extra
                b.calls += 1
                self._flag(b, extra, now)
        elif res.state == "expired" and not res.late_settled:
            res.late_settled = True  # the sweeper's calls row is rewritten once
            for b in self.budgets.values():
                b.spent += extra
                b.overrun += extra
                self._flag(b, extra, now)

    @staticmethod
    def _flag(b: ModelBudget, extra: int, now: int) -> None:
        if extra > 0 and b.strict and b.spent + b.held > b.effective(now):
            b.review = True

    def release(self, rid: str) -> None:
        res = self.reservations[rid]
        if res.state == "pending":
            res.state = "released"
            for b in self.budgets.values():
                b.held -= res.held

    def sweep(self, now: int) -> list[str]:
        expired = [
            r for r, x in self.reservations.items() if x.state == "pending" and x.lease < now
        ]
        for rid in expired:
            res = self.reservations[rid]
            res.state = "expired"
            for b in self.budgets.values():
                b.held -= res.held
                b.spent += res.held
                b.calls += 1
        for b in self.budgets.values():
            if any(end <= now for _, _, end in b.grants):
                b.excess = max(b.excess, b.needed_excess(now))
        return sorted(expired)

    def renew(self, owner: str, ids: set[str], now: int, lease_ms: int) -> list[str]:
        renewed = [
            r
            for r, x in self.reservations.items()
            if r in ids and x.owner == owner and x.state == "pending"
        ]
        for rid in renewed:
            self.reservations[rid].lease = now + lease_ms
        return sorted(renewed)

    def set_cap(self, name: str, cap: int, now: int) -> None:
        b = self.budgets[name]
        b.cap = cap
        b.excess = b.needed_excess(now)  # may shrink: room given back

    def add_grant(self, name: str, amount: int, start: int, end: int, now: int) -> None:
        b = self.budgets[name]
        b.grants.append((amount, start, end))
        b.excess = b.needed_excess(now)

    def ack(self, name: str) -> None:
        self.budgets[name].review = False
