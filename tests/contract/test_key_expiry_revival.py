"""A key expiry can be moved earlier, never undone, on a key a rotation replaced or that expired.

Rotation sets the old key's expiry to the end of its grace period, the documented answer to a
leaked secret (threat model TH-01). Clearing or extending that expiry, or the expiry of a key that
has already expired, would make the old secret spend again (SG-PT-10), so the ledger refuses it in
the same transaction that locks the key row: on both backends, and whoever calls it.
"""

import pytest

from spendgate.ledger.admin_port import AdminLedger
from spendgate.ledger.key_types import KeySpec
from spendgate.ledger.port import ConflictError
from tests.contract.rig import T0, Rig

PEPPER = b"contract-pepper-0123456789abcdef"
MINUTE = 60_000
DAY = 86_400_000


def led(rig: Rig) -> AdminLedger:
    return rig.ledger


def _key(rig: Rig, expires_at: int | None = None) -> str:
    w = rig.world("10")
    spec = KeySpec(w.scope.id, "k", expires_at=expires_at)
    return rig.run(led(rig).create_key(spec, pepper=PEPPER, actor="adm", now=T0)).record.id


def _expiry(rig: Rig, key_id: str) -> int | None:
    stored = rig.run(led(rig).get_key(key_id))
    assert stored is not None
    return stored.expires_at


def _updates(rig: Rig) -> int:
    rows = rig.run(led(rig).list_audit(since=0, until=None, actor="adm", before=None, limit=50))
    return sum(1 for r in rows if r.action == "key.update")


def test_a_rotated_out_key_cannot_be_revived_by_patch(rig: Rig) -> None:
    old = _key(rig)
    rig.run(led(rig).rotate_key(old, grace_ms=10 * MINUTE, pepper=PEPPER, actor="adm", now=T0))
    until = T0 + 10 * MINUTE
    for revived in (None, until + 1, until + DAY):
        with pytest.raises(ConflictError, match="replaced by rotation"):
            rig.run(led(rig).set_key_expiry(old, revived, actor="adm", now=T0 + 1))
    assert _expiry(rig, old) == until and _updates(rig) == 0
    # Inside the grace period it can still be ended sooner, or kept as it is.
    rig.run(led(rig).set_key_expiry(old, until, actor="adm", now=T0 + 2))
    earlier = rig.run(led(rig).set_key_expiry(old, T0 + MINUTE, actor="adm", now=T0 + 3))
    assert earlier.expires_at == T0 + MINUTE == _expiry(rig, old)


def test_an_expired_key_cannot_be_revived_by_patch(rig: Rig) -> None:
    key = _key(rig, expires_at=T0 + DAY)
    after = T0 + 2 * DAY
    for revived in (None, T0 + DAY + 1, after + DAY):
        with pytest.raises(ConflictError, match="has expired"):
            rig.run(led(rig).set_key_expiry(key, revived, actor="adm", now=after))
    assert _expiry(rig, key) == T0 + DAY and _updates(rig) == 0
    moved = rig.run(led(rig).set_key_expiry(key, T0 + 1, actor="adm", now=after))
    assert moved.expires_at == T0 + 1


def test_a_key_expiring_exactly_now_counts_as_expired(rig: Rig) -> None:
    key = _key(rig, expires_at=T0 + DAY)
    with pytest.raises(ConflictError, match="has expired"):
        rig.run(led(rig).set_key_expiry(key, None, actor="adm", now=T0 + DAY))


def test_an_active_keys_expiry_can_still_be_set_moved_earlier_or_cleared(rig: Rig) -> None:
    key = _key(rig)
    steps = (T0 + 2 * DAY, T0 + DAY, T0 + 3 * DAY, None)  # set, earlier, later, cleared
    for n, when in enumerate(steps, start=1):
        rec = rig.run(led(rig).set_key_expiry(key, when, actor="adm", now=T0 + n))
        assert rec.expires_at == when == _expiry(rig, key)
    assert _updates(rig) == len(steps)
