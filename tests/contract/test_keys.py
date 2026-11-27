"""Key lifecycle on every adapter: C-01 and C-03 of the threat model.

T-AUTH-01 no plaintext secret in the DB or logs; T-AUTH-03 revoke and rotate without restart;
T-AUTH-04 constant-time compare; T-ADM-01 (store level) admin scopes; T-ADM-02 (store level) an
api key never works as an admin key and the reverse; T-ADM-04 audit row per key change.
"""

import hmac
import logging
import secrets
import sqlite3
from collections.abc import Callable

import pytest

from spendgate.core.budgets import ScopeKind
from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.policy import KeyPolicy, KillSwitch, KillTarget
from spendgate.core.sessions import SessionDefaults
from spendgate.ledger.key_types import AdminScope, KeyKind, KeySpec, NewKey, Principal
from spendgate.ledger.keys import KeyService
from spendgate.ledger.port import ConflictError, Held, NotFoundError
from spendgate.ledger.sql import Value
from tests.contract.rig import T0, E, Rig
from tests.contract.test_ledger import refusal_of

PEPPER = b"test-pepper-0123456789abcdef"
TTL_MS = 5_000
GRACE_MS = 60_000


def service(rig: Rig, *, ttl_ms: int = TTL_MS, max_entries: int = 1_024) -> KeyService:
    return KeyService(rig.ledger, PEPPER, ttl_ms=ttl_ms, max_entries=max_entries)


def make_scope(rig: Rig) -> str:
    org = rig.run(rig.ledger.create_scope(ScopeKind.ORG, "acme", None, now=T0))
    return org.id


def api_key(rig: Rig, svc: KeyService, scope_id: str, **kw: object) -> NewKey:
    spec = KeySpec(scope_id, "app", **kw)  # type: ignore[arg-type]
    return rig.run(svc.create_key(spec, actor="admin", now=T0))


def admin_key(rig: Rig, scope_id: str, *scopes: AdminScope) -> NewKey:
    return rig.run(
        rig.ledger.create_admin_key(
            scope_id, "ops", frozenset(scopes), pepper=PEPPER, actor="boot", now=T0
        )
    )


def principal(found: Principal | Refusal) -> Principal:
    assert isinstance(found, Principal), found
    return found


def refused(found: Principal | Refusal, code: ErrorCode = ErrorCode.UNAUTHORIZED) -> None:
    assert isinstance(found, Refusal), found
    assert found.code is code


def sqlite_rows(rig: Rig, sql: str) -> list[tuple[Value, ...]]:
    if rig.kind != "sqlite":
        pytest.skip("direct table reads are only wired for SQLite until the Postgres hook has one")
    conn = sqlite3.connect(f"file:{rig.location}?mode=ro", uri=True)
    try:
        return [tuple(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


# --- create, list, use -----------------------------------------------------------------------


def test_create_verify_and_list_never_return_the_secret(rig: Rig) -> None:
    svc = service(rig)
    scope = make_scope(rig)
    made = api_key(rig, svc, scope, rpm=10, allowed_models=("gpt-*",))
    assert made.secret.startswith("sg-")
    assert made.secret not in repr(made) and made.secret not in repr(made.record)
    who = principal(rig.run(svc.verify_api(made.secret, T0)))
    assert (who.key_id, who.scope_id, who.kind) == (made.record.id, scope, KeyKind.API)
    assert who.policy.rpm == 10 and who.policy.allowed_models == ("gpt-*",)
    listed = rig.run(rig.ledger.list_keys(scope))
    assert [k.id for k in listed] == [made.record.id]
    assert made.secret.split("-")[2] not in repr(listed)
    assert rig.run(rig.ledger.list_keys(make_other(rig))) == ()


def make_other(rig: Rig) -> str:
    return rig.run(rig.ledger.create_scope(ScopeKind.ORG, "other", None, now=T0)).id


def test_wrong_secret_and_unknown_prefix_are_refused_alike(rig: Rig) -> None:
    svc = service(rig)
    made = api_key(rig, svc, make_scope(rig))
    _, prefix, secret = made.secret.split("-")
    flipped = ("0" if secret[0] != "0" else "1") + secret[1:]
    refused(rig.run(svc.verify_api(f"sg-{prefix}-{flipped}", T0)))
    refused(rig.run(svc.verify_api(f"sg-{'f' * 12}-{secret}", T0)))
    # same refusal object for every failure: nothing reveals which part was wrong
    a = rig.run(svc.verify_api(f"sg-{prefix}-{flipped}", T0))
    b = rig.run(svc.verify_api(f"sg-{'f' * 12}-{secret}", T0))
    assert a == b


def test_unknown_scope_is_not_found(rig: Rig) -> None:
    with pytest.raises(NotFoundError):
        rig.run(service(rig).create_key(KeySpec("no-such-scope", "x"), actor="a", now=T0))


def test_a_stored_key_can_hold_budget(rig: Rig) -> None:
    """The reservation foreign key accepts a real key (replaces the `add_key_record` stub)."""
    svc = service(rig)
    world = rig.world("5")
    made = api_key(rig, svc, world.scope.id)
    policy = principal(rig.run(svc.verify_api(made.secret, T0))).policy
    assert isinstance(rig.hold(world, "r1", "1", key=policy), Held)


def test_expired_key_is_refused(rig: Rig) -> None:
    svc = service(rig)
    made = api_key(rig, svc, make_scope(rig), expires_at=T0 + 1_000)
    principal(rig.run(svc.verify_api(made.secret, T0 + 999)))
    refused(rig.run(svc.verify_api(made.secret, T0 + 1_000)))


# --- revocation and rotation (T-AUTH-03) -----------------------------------------------------


def test_revoked_key_is_refused_at_once_in_the_same_process(rig: Rig) -> None:
    svc = service(rig)
    made = api_key(rig, svc, make_scope(rig))
    principal(rig.run(svc.verify_api(made.secret, T0)))  # now cached
    rig.run(svc.revoke_key(made.record.id, actor="admin", now=T0 + 1))
    refused(rig.run(svc.verify_api(made.secret, T0 + 2)))  # well inside the TTL


def test_revoke_elsewhere_is_seen_within_the_cache_ttl(rig: Rig) -> None:
    """Another instance's cache keeps a revoked key for at most `ttl_ms` (the documented bound)."""
    svc = service(rig)
    made = api_key(rig, svc, make_scope(rig))
    principal(rig.run(svc.verify_api(made.secret, T0)))
    rig.run(rig.ledger.revoke_key(made.record.id, actor="other-instance", now=T0 + 1))
    principal(rig.run(svc.verify_api(made.secret, T0 + TTL_MS - 1)))  # stale cache entry
    refused(rig.run(svc.verify_api(made.secret, T0 + TTL_MS)))  # entry aged out, DB says revoked


def test_revoke_twice_keeps_the_first_time_and_one_audit_row(rig: Rig) -> None:
    made = api_key(rig, service(rig), make_scope(rig))
    first = rig.run(rig.ledger.revoke_key(made.record.id, actor="a", now=T0 + 5))
    again = rig.run(rig.ledger.revoke_key(made.record.id, actor="a", now=T0 + 9))
    assert first.revoked_at == again.revoked_at == T0 + 5
    rows = sqlite_rows(rig, "SELECT action FROM admin_audit WHERE action = 'key.revoke'")
    assert len(rows) == 1


def test_rotation_grace_period(rig: Rig) -> None:
    svc = service(rig, ttl_ms=10 * GRACE_MS)  # cache longer than the grace: expiry still exact
    made = api_key(rig, svc, make_scope(rig), rpm=7)
    principal(rig.run(svc.verify_api(made.secret, T0)))
    fresh = rig.run(svc.rotate_key(made.record.id, grace_ms=GRACE_MS, actor="admin", now=T0))
    assert fresh.record.replaces_key_id == made.record.id and fresh.secret != made.secret
    assert fresh.record.policy.rpm == 7 and fresh.record.scope_id == made.record.scope_id
    both = (T0 + GRACE_MS - 1, fresh.secret), (T0 + GRACE_MS - 1, made.secret)
    for at, secret in both:
        principal(rig.run(svc.verify_api(secret, at)))
    refused(rig.run(svc.verify_api(made.secret, T0 + GRACE_MS)))
    principal(rig.run(svc.verify_api(fresh.secret, T0 + GRACE_MS)))


def test_rotation_with_zero_grace_cuts_the_old_key_at_once(rig: Rig) -> None:
    svc = service(rig)
    made = api_key(rig, svc, make_scope(rig))
    rig.run(svc.rotate_key(made.record.id, grace_ms=0, actor="admin", now=T0))
    refused(rig.run(svc.verify_api(made.secret, T0)))


def test_a_revoked_key_cannot_be_rotated(rig: Rig) -> None:
    svc = service(rig)
    made = api_key(rig, svc, make_scope(rig))
    rig.run(svc.revoke_key(made.record.id, actor="admin", now=T0))
    with pytest.raises(ConflictError):
        rig.run(svc.rotate_key(made.record.id, grace_ms=GRACE_MS, actor="admin", now=T0))


# --- M-18: a rotated key inherits its key kill switch (ADR-0030) -----------------------------


def test_m18_a_rotated_key_inherits_its_key_kill_switch(rig: Rig) -> None:
    svc = service(rig)
    world = rig.world("100")
    made = api_key(rig, svc, world.scope.id)
    switch = KillSwitch(KillTarget.KEY, made.record.id)
    switch_id = rig.run(rig.ledger.engage_kill_switch(switch, actor="a", reason="r", now=T0))
    fresh = rig.run(svc.rotate_key(made.record.id, grace_ms=GRACE_MS, actor="k", now=T0))
    again = rig.run(svc.rotate_key(fresh.record.id, grace_ms=GRACE_MS, actor="k", now=T0))
    for n, key in enumerate((made, fresh, again)):  # the whole chain, however long
        policy = principal(rig.run(svc.verify_api(key.secret, T0))).policy
        assert refusal_of(rig.hold(world, f"k{n}", "1", key=policy)) is ErrorCode.KILL_SWITCH
    rig.run(rig.ledger.lift_kill_switch(switch_id, actor="a", now=T0))  # lifts it for the chain
    for n, key in enumerate((made, fresh, again)):
        policy = principal(rig.run(svc.verify_api(key.secret, T0))).policy
        held = rig.hold(world, f"ok{n}", "1", key=policy)
        assert isinstance(held, Held), held


def test_m18_a_hold_reports_the_keys_its_key_replaced(rig: Rig) -> None:
    """The lineage the admission read rides on the hold, so a stream's watch can apply it."""
    svc = service(rig)
    world = rig.world("100")
    made = api_key(rig, svc, world.scope.id)
    fresh = rig.run(svc.rotate_key(made.record.id, grace_ms=GRACE_MS, actor="k", now=T0))
    again = rig.run(svc.rotate_key(fresh.record.id, grace_ms=GRACE_MS, actor="k", now=T0))
    held = rig.hold(world, "r1", "1", key=again.record.policy)
    assert isinstance(held, Held)
    assert set(held.key_lineage) == {made.record.id, fresh.record.id}
    first = rig.hold(world, "r2", "1", key=made.record.policy)
    assert isinstance(first, Held) and first.key_lineage == ()


def test_m18_a_kill_switch_on_a_sibling_key_does_not_apply(rig: Rig) -> None:
    svc = service(rig)
    world = rig.world("100")
    made = api_key(rig, svc, world.scope.id)
    other = api_key(rig, svc, world.scope.id)
    switch = KillSwitch(KillTarget.KEY, other.record.id)
    rig.run(rig.ledger.engage_kill_switch(switch, actor="a", reason="r", now=T0))
    fresh = rig.run(svc.rotate_key(made.record.id, grace_ms=GRACE_MS, actor="k", now=T0))
    assert isinstance(rig.hold(world, "r1", "1", key=fresh.record.policy), Held)


# --- policy ----------------------------------------------------------------------------------


def test_set_policy_round_trips_every_field_and_takes_effect(rig: Rig) -> None:
    svc = service(rig)
    scope = make_scope(rig)
    made = api_key(rig, svc, scope)
    principal(rig.run(svc.verify_api(made.secret, T0)))  # cached with the old policy
    sessions = SessionDefaults(E("1"), 20, 600, 60, 3, required=True)
    wanted = KeyPolicy(
        made.record.id, scope, ("gpt-*", "claude-*"), 4096, E("2"), 60, 90_000, 4, sessions
    )
    rig.run(svc.set_key_policy(wanted, name="renamed", actor="admin", now=T0 + 1))
    assert principal(rig.run(svc.verify_api(made.secret, T0 + 2))).policy == wanted
    stored = rig.run(rig.ledger.get_key(made.record.id))
    assert stored is not None and stored.name == "renamed" and stored.policy == wanted


# T-KEY-01
def test_set_policy_empty_models_is_refused_none_means_all_and_scope_cannot_move(rig: Rig) -> None:
    svc = service(rig)
    scope = make_scope(rig)
    made = api_key(rig, svc, scope, allowed_models=("a",))
    with pytest.raises(ValueError, match="must not be empty"):  # SR-09: never a silent widening
        rig.run(
            svc.set_key_policy(
                KeyPolicy(made.record.id, scope, allowed_models=()), actor="a", now=T0
            )
        )
    with pytest.raises(ValueError, match="must not be empty"):
        api_key(rig, svc, scope, allowed_models=())
    cleared = KeyPolicy(made.record.id, scope, allowed_models=None)
    assert rig.run(svc.set_key_policy(cleared, actor="a", now=T0)).policy.allowed_models is None
    with pytest.raises(ValueError, match="scope"):
        rig.run(svc.set_key_policy(KeyPolicy(made.record.id, "other"), actor="a", now=T0))
    with pytest.raises(NotFoundError):
        rig.run(svc.set_key_policy(KeyPolicy("missing", scope), actor="a", now=T0))


def test_every_key_change_writes_an_audit_row(rig: Rig) -> None:
    svc = service(rig)
    scope = make_scope(rig)
    made = api_key(rig, svc, scope)
    rig.run(svc.set_key_policy(KeyPolicy(made.record.id, scope, rpm=5), actor="a", now=T0))
    rig.run(svc.rotate_key(made.record.id, grace_ms=1, actor="a", now=T0))
    rig.run(svc.revoke_key(made.record.id, actor="a", now=T0))
    rows = sqlite_rows(
        rig, "SELECT action, actor_key_id FROM admin_audit WHERE action LIKE 'key.%'"
    )
    assert sorted(str(a) for a, _ in rows) == [
        "key.create", "key.create", "key.revoke", "key.rotate", "key.update",
    ]  # fmt: skip
    assert {who for _, who in rows} == {"admin", "a"}


# --- secrets at rest and in logs (T-AUTH-01) -------------------------------------------------


def test_secret_never_stored_or_logged(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    svc = service(rig)
    scope = make_scope(rig)
    made = [api_key(rig, svc, scope), admin_key(rig, scope, AdminScope.KEYS_WRITE)]
    made.append(rig.run(svc.rotate_key(made[0].record.id, grace_ms=1, actor="a", now=T0)))
    for key in made:
        rig.run(svc.verify_api(key.secret, T0))
        rig.run(svc.verify_admin(key.secret, T0, AdminScope.KEYS_WRITE))
    rig.run(svc.revoke_key(made[2].record.id, actor="a", now=T0))
    needles = [k.secret for k in made] + [k.secret.split("-")[2] for k in made] + [PEPPER.decode()]
    for table in [
        r[0] for r in sqlite_rows(rig, "SELECT name FROM sqlite_master WHERE type='table'")
    ]:
        for row in sqlite_rows(rig, f"SELECT * FROM {table}"):  # noqa: S608 - names from sqlite_master
            cells = " ".join(str(c) for c in row if isinstance(c, str | bytes))
            assert not [n for n in needles if n in cells], table
    assert not [n for n in needles if n in caplog.text]
    stored = rig.run(rig.ledger.find_key(made[0].record.prefix))
    assert stored is not None and stored.secret_hash not in repr(stored)


# --- constant-time compare and cheap refusals (T-AUTH-04) ------------------------------------


def test_hash_comparison_uses_compare_digest(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    svc = service(rig)
    made = api_key(rig, svc, make_scope(rig))
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    principal(rig.run(svc.verify_api(made.secret, T0)))
    assert len(calls) == 1 and calls[0][0] == calls[0][1]
    refused(
        rig.run(svc.verify_api(f"sg-{'e' * 12}-{'d' * 48}", T0))
    )  # unknown prefix: compares too
    assert len(calls) == 2 and calls[1][0] != calls[1][1]


MALFORMED = [
    "",
    "sg-",
    "sg-abc-def",
    "sgx-" + "a" * 12 + "-" + "b" * 48,
    "sg-" + "A" * 12 + "-" + "b" * 48,  # uppercase is not our alphabet
    "sg-" + "a" * 12 + "-" + "b" * 47,
    "sg-" + "a" * 12 + "-" + "b" * 49,
    " sg-" + "a" * 12 + "-" + "b" * 48,
    "Bearer sg-" + "a" * 12 + "-" + "b" * 48,
    "sg-" + "a" * 12 + "-" + "b" * 48 + "\n",
    "sg-é" + "a" * 11 + "-" + "b" * 48,
    "sg-" + "a" * 12 + "-" + "b" * 10_000,
]


@pytest.mark.parametrize("raw", MALFORMED)
def test_malformed_keys_are_refused_without_touching_the_store(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    hits: list[str] = []

    async def counting(prefix: str) -> None:
        hits.append(prefix)

    monkeypatch.setattr(rig.ledger, "find_key", counting)
    refused(rig.run(service(rig).verify_api(raw, T0)))
    refused(rig.run(service(rig).verify_admin(raw, T0, AdminScope.USAGE_READ)))
    assert hits == []


def test_cache_serves_repeats_and_evicts_least_recently_used(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = service(rig, max_entries=2)
    scope = make_scope(rig)
    keys = [api_key(rig, svc, scope) for _ in range(3)]
    real = rig.ledger.find_key
    looked_up: list[str] = []

    async def counting(prefix: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
        looked_up.append(prefix)
        return await real(prefix)

    monkeypatch.setattr(rig.ledger, "find_key", counting)
    for key in (keys[0], keys[0], keys[1], keys[2], keys[1]):
        principal(rig.run(svc.verify_api(key.secret, T0)))
    assert looked_up == [k.record.prefix for k in (keys[0], keys[1], keys[2])]  # 0 evicted by 2
    principal(rig.run(svc.verify_api(keys[0].secret, T0)))
    assert looked_up[-1] == keys[0].record.prefix


# --- admin keys (T-ADM-01, T-ADM-02 at the store level) --------------------------------------


def test_admin_key_scopes_are_enforced(rig: Rig) -> None:
    svc = service(rig)
    scope = make_scope(rig)
    reader = admin_key(rig, scope, AdminScope.USAGE_READ)
    manager = admin_key(rig, scope, AdminScope.KEYS_WRITE, AdminScope.BUDGETS_WRITE)
    assert reader.secret.startswith("sga-")
    who = principal(rig.run(svc.verify_admin(reader.secret, T0, AdminScope.USAGE_READ)))
    assert who.kind is KeyKind.ADMIN and who.admin_scopes == {AdminScope.USAGE_READ}
    for needed in (AdminScope.KEYS_WRITE, AdminScope.BUDGETS_WRITE):
        refused(rig.run(svc.verify_admin(reader.secret, T0, needed)), ErrorCode.FORBIDDEN)
    principal(rig.run(svc.verify_admin(manager.secret, T0, AdminScope.KEYS_WRITE)))
    principal(rig.run(svc.verify_admin(manager.secret, T0, AdminScope.BUDGETS_WRITE)))
    refused(
        rig.run(svc.verify_admin(manager.secret, T0, AdminScope.USAGE_READ)), ErrorCode.FORBIDDEN
    )


def test_api_and_admin_keys_never_cross_planes(rig: Rig) -> None:
    svc = service(rig)
    scope = make_scope(rig)
    virtual = api_key(rig, svc, scope)
    admin = admin_key(rig, scope, AdminScope.USAGE_READ)
    refused(rig.run(svc.verify_admin(virtual.secret, T0, AdminScope.USAGE_READ)))
    refused(rig.run(svc.verify_api(admin.secret, T0)))
    # same prefix and secret behind the other tag is not a different valid key either
    swapped = admin.secret.replace("sga-", "sg-", 1)
    refused(rig.run(svc.verify_api(swapped, T0)))


def test_revoked_admin_key_is_refused_and_listing_separates_kinds(rig: Rig) -> None:
    svc = service(rig)
    scope = make_scope(rig)
    admin = admin_key(rig, scope, AdminScope.KEYS_WRITE)
    api_key(rig, svc, scope)
    rig.run(svc.revoke_key(admin.record.id, actor="boot", now=T0))
    refused(rig.run(svc.verify_admin(admin.secret, T0 + 1, AdminScope.KEYS_WRITE)))
    admins = rig.run(rig.ledger.list_keys(kind=KeyKind.ADMIN))
    assert [k.id for k in admins] == [admin.record.id]
    assert len(rig.run(rig.ledger.list_keys())) == 1


def test_admin_key_spec_rules(new_id: Callable[[], str]) -> None:
    with pytest.raises(ValueError, match="admin scope"):
        KeySpec("s", "x", KeyKind.ADMIN)
    with pytest.raises(ValueError, match="admin scope"):
        KeySpec("s", "x", KeyKind.API, admin_scopes=frozenset({AdminScope.KEYS_WRITE}))


class _CountingLedger:
    """Counts `find_key` reads and forwards everything else (a stand-in for ledger load)."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.reads = 0

    async def find_key(self, prefix: str) -> object:
        self.reads += 1
        return await self._inner.find_key(prefix)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def test_t_auth_05_a_repeated_unknown_key_costs_one_ledger_read_per_window(rig: Rig) -> None:
    """SR-11: bogus well-formed keys used to cost one ledger read each, forever."""
    counting = _CountingLedger(rig.ledger)
    svc = KeyService(counting, PEPPER, ttl_ms=TTL_MS)  # type: ignore[arg-type]
    bogus = f"sg-{'a' * 12}-{'b' * 48}"
    for _ in range(50):
        refused(rig.run(svc.verify_api(bogus, T0)))
    assert counting.reads == 1
    refused(rig.run(svc.verify_api(bogus, T0 + 999)))
    assert counting.reads == 1  # still inside the (short) negative window
    refused(rig.run(svc.verify_api(bogus, T0 + 1_001)))
    assert counting.reads == 2  # the window ended: asked again
    for _ in range(200):  # a flood of distinct prefixes stays bounded in memory
        refused(rig.run(svc.verify_api(f"sg-{secrets.token_hex(6)}-{'b' * 48}", T0)))
    assert len(svc._misses) <= 1_024


def test_t_auth_05_a_key_created_after_a_miss_works_at_once(rig: Rig) -> None:
    svc = service(rig)
    scope = make_scope(rig)
    made = api_key(rig, svc, scope)
    _, prefix, secret = made.secret.split("-")
    other = service(rig)  # another process's view: it already remembered the prefix as unknown
    other._misses[prefix] = T0
    refused(rig.run(other.verify_api(made.secret, T0 + 10)))  # the documented <= 1 s lag
    made2 = rig.run(svc.create_key(KeySpec(scope, "second"), actor="admin", now=T0))
    principal(rig.run(svc.verify_api(made2.secret, T0 + 10)))  # same service: no lag at all
    assert secret  # (the first key's secret is only used to take the prefix apart)
