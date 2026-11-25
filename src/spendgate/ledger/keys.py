"""Virtual and admin key material, verification and the in-process lookup cache.

Format (scannable, fixed width, lowercase hex; a secret scanner can match it with a regex):

    data plane   sg-<prefix>-<secret>     \\bsg-[0-9a-f]{12}-[0-9a-f]{48}\\b
    admin plane  sga-<prefix>-<secret>    \\bsga-[0-9a-f]{12}-[0-9a-f]{48}\\b

`prefix` is a public random id (48 bits, unique index) used to find the row; `secret` is 192
random bits from `secrets`. Only the prefix and `HMAC-SHA256(pepper, secret)` are stored. HMAC
rather than `SHA-256(secret || pepper)`: it is the standard keyed construction (no length
extension, no concatenation ambiguity) and costs the same. A slow KDF is not needed because the
secret is 192 random bits, not a human password. The pepper comes from the caller (config:
`env:`/`file:`), is never written to the database, and without it a stolen ledger cannot be used
to check guesses.

Cache: `KeyService` keeps the stored row (never the secret) per prefix for `ttl_ms`, so a hot
key costs no query. The hash comparison, expiry and kind checks run on every request, so a
rotation grace period ends exactly on time. A revoke or rotate made through this service drops
the entry at once; another process notices it at most `ttl_ms` later, which is the documented
revocation bound (T-AUTH-03). Malformed keys never reach the ledger. A well-formed prefix the
ledger does not know (or that is revoked) is remembered for `min(ttl_ms, 1 s)`, so a client
retrying one bad key in a loop costs one read a second, not one per request (SR-11); a flood of
distinct random prefixes still costs a read each. A key created through this service clears the
negative entries. Single event loop only: the caches have no lock.
"""

import hashlib
import hmac
import re
import secrets
from collections import OrderedDict
from dataclasses import dataclass
from typing import Final

from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.pepper import MIN_PEPPER_BYTES
from spendgate.core.policy import KeyPolicy
from spendgate.ledger.admin_port import AdminLedger
from spendgate.ledger.key_types import (
    AdminScope,
    KeyKind,
    KeyRecord,
    KeySpec,
    NewKey,
    Principal,
    StoredKey,
)

PREFIX_HEX: Final = 12
SECRET_HEX: Final = 48
_KIND_TAG: Final = {KeyKind.API: "sg", KeyKind.ADMIN: "sga"}
_KEY = re.compile(rf"(sga?)-([0-9a-f]{{{PREFIX_HEX}}})-([0-9a-f]{{{SECRET_HEX}}})")
_MAX_KEY_LEN: Final = 4 + PREFIX_HEX + 1 + SECRET_HEX
_UNAUTHORIZED: Final = Refusal(ErrorCode.UNAUTHORIZED, "Invalid or missing API key.")
_FORBIDDEN: Final = Refusal(ErrorCode.FORBIDDEN, "This key may not do that.")
_MISS_TTL_MS: Final = 1_000
# Compared against when the prefix is unknown, so a miss costs the same compare as a hit.
_DUMMY_HASH: Final = "0" * 64


@dataclass(frozen=True, slots=True)
class ParsedKey:
    kind: KeyKind
    prefix: str
    secret: str


def new_prefix() -> str:
    return secrets.token_hex(PREFIX_HEX // 2)


def new_secret() -> str:
    return secrets.token_hex(SECRET_HEX // 2)


def format_key(kind: KeyKind, prefix: str, secret: str) -> str:
    return f"{_KIND_TAG[kind]}-{prefix}-{secret}"


def parse_key(raw: str) -> ParsedKey | None:
    """Shape check only, no I/O: anything else is refused before a database is touched."""
    if len(raw) > _MAX_KEY_LEN:
        return None
    match = _KEY.fullmatch(raw)
    if match is None:
        return None
    kind = KeyKind.ADMIN if match[1] == "sga" else KeyKind.API
    return ParsedKey(kind, match[2], match[3])


def hash_secret(secret: str, pepper: bytes) -> str:
    # The config already refuses a short pepper (exit 3); this guards every other caller.
    if len(pepper) < MIN_PEPPER_BYTES:
        raise ValueError(f"the key pepper must be at least {MIN_PEPPER_BYTES} bytes")
    return hmac.new(pepper, secret.encode(), hashlib.sha256).hexdigest()


class KeyService:
    """Create, verify and change keys over any ledger; owns the pepper and the lookup cache."""

    def __init__(
        self,
        ledger: AdminLedger,
        pepper: bytes,
        *,
        ttl_ms: int = 5_000,
        max_entries: int = 1_024,
    ) -> None:
        hash_secret("", pepper)  # validates the pepper at start, not on the first request
        if ttl_ms < 0 or max_entries < 1:
            raise ValueError("ttl_ms must not be negative and max_entries must be at least 1")
        self._ledger = ledger
        self._pepper = pepper
        self._ttl_ms = ttl_ms
        self._max = max_entries
        self._cache: OrderedDict[str, tuple[StoredKey, int]] = OrderedDict()
        self._misses: OrderedDict[str, int] = OrderedDict()  # prefix -> when it was not found
        # key id -> prefix, for `still_active`. A key's prefix never changes (rotation makes a new
        # key), so an entry can only be missing, never wrong; it is bounded like the cache.
        self._prefixes: OrderedDict[str, str] = OrderedDict()

    # --- verification ---

    async def verify_api(self, raw: str, now: int) -> Principal | Refusal:
        """A data-plane key to its principal, or `unauthorized` (never says why)."""
        return await self._verify(raw, now, KeyKind.API)

    async def verify_admin(self, raw: str, now: int, required: AdminScope) -> Principal | Refusal:
        """An admin key holding `required`: else `unauthorized` (bad key) or `forbidden` (scope)."""
        found = await self._verify(raw, now, KeyKind.ADMIN)
        if isinstance(found, Refusal) or required in found.admin_scopes:
            return found
        return _FORBIDDEN

    async def _verify(self, raw: str, now: int, kind: KeyKind) -> Principal | Refusal:
        parsed = parse_key(raw)
        if parsed is None or parsed.kind is not kind:
            return _UNAUTHORIZED
        stored = await self._lookup(parsed.prefix, now)
        expected = _DUMMY_HASH if stored is None else stored.secret_hash
        presented = hash_secret(parsed.secret, self._pepper)
        matches = hmac.compare_digest(expected.encode(), presented.encode())
        if stored is None or not matches:
            return _UNAUTHORIZED
        rec = stored.record
        if rec.kind is not kind or rec.revoked_at is not None:
            return _UNAUTHORIZED
        if rec.expires_at is not None and now >= rec.expires_at:
            return _UNAUTHORIZED
        return Principal(rec.id, rec.kind, rec.scope_id, rec.policy, rec.admin_scopes)

    async def still_active(self, principal: Principal, now: int) -> bool:
        """Whether `principal`'s key is still unrevoked and unexpired, through the cached lookup.

        Not a second authentication: no secret is compared. A revoke through this service is seen
        at once; one made elsewhere within `ttl_ms`, the same bound as `verify_api`.
        """
        prefix = self._prefixes.get(principal.key_id)
        if prefix is None:  # pushed out of the bounded map meanwhile: read the record by id
            record = await self._ledger.get_key(principal.key_id)
        else:
            stored = await self._lookup(prefix, now)
            record = stored.record if stored is not None else None
        if record is None or record.id != principal.key_id or record.kind is not principal.kind:
            return False
        expired = record.expires_at is not None and now >= record.expires_at
        return record.revoked_at is None and not expired

    async def _lookup(self, prefix: str, now: int) -> StoredKey | None:
        hit = self._cache.get(prefix)
        if hit is not None and 0 <= now - hit[1] < self._ttl_ms:
            self._cache.move_to_end(prefix)
            return hit[0]
        self._cache.pop(prefix, None)
        missed = self._misses.get(prefix)
        if missed is not None and 0 <= now - missed < min(self._ttl_ms, _MISS_TTL_MS):
            return None
        self._misses.pop(prefix, None)
        stored = await self._ledger.find_key(prefix)
        if stored is not None and stored.record.revoked_at is None:
            self._cache[prefix] = (stored, now)
            self._prefixes[stored.record.id] = prefix
            self._prefixes.move_to_end(stored.record.id)
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
            while len(self._prefixes) > self._max:
                self._prefixes.popitem(last=False)
        elif self._ttl_ms > 0:  # unknown or revoked: remember briefly, bounded like the cache
            self._misses[prefix] = now
            while len(self._misses) > self._max:
                self._misses.popitem(last=False)
        return stored

    def invalidate(self, key_id: str) -> None:
        for prefix in [p for p, (s, _) in self._cache.items() if s.record.id == key_id]:
            del self._cache[prefix]

    # --- admin operations: the ledger does the transaction, this drops stale cache entries ---

    async def create_key(self, spec: KeySpec, *, actor: str, now: int) -> NewKey:
        self._misses.clear()  # a prefix that was unknown a moment ago may be this new key
        return await self._ledger.create_key(spec, pepper=self._pepper, actor=actor, now=now)

    async def revoke_key(self, key_id: str, *, actor: str, now: int) -> KeyRecord:
        try:
            return await self._ledger.revoke_key(key_id, actor=actor, now=now)
        finally:
            self.invalidate(key_id)

    async def rotate_key(self, key_id: str, *, grace_ms: int, actor: str, now: int) -> NewKey:
        self._misses.clear()
        try:
            return await self._ledger.rotate_key(
                key_id, grace_ms=grace_ms, pepper=self._pepper, actor=actor, now=now
            )
        finally:
            self.invalidate(key_id)

    async def set_key_policy(
        self, policy: KeyPolicy, *, name: str | None = None, actor: str, now: int
    ) -> KeyRecord:
        try:
            return await self._ledger.set_key_policy(policy, name=name, actor=actor, now=now)
        finally:
            self.invalidate(policy.id)
