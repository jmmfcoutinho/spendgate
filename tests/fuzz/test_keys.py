"""Target 8: `ledger.keys.parse_key`, `KeyService._verify` and `pipeline.auth.presented_key`.

Inputs: arbitrary text, and real keys mutated one step at a time (a changed or case-flipped digit,
a swapped kind tag, trailing newline / space / NUL, a Unicode look-alike, a doubled or dropped
segment), plus header mappings with `Authorization` / `X-Api-Key` in every spelling.

Invariants: only the exact documented formats parse (`sg-` / `sga-`, 12 + 48 lower-case ASCII hex);
nothing raises; an admin key never verifies as an API key and an API key never verifies as an admin
key; a revoked or expired key never verifies; a verified principal is exactly the key that was
presented; `presented_key` returns a key that was actually presented (stripped), refuses two
different keys, and never returns an empty one.
"""

import asyncio
import re
from typing import cast

from hypothesis import strategies as st

from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.policy import KeyPolicy
from spendgate.ledger.admin_port import AdminLedger
from spendgate.ledger.key_types import AdminScope, KeyKind, KeyRecord, Principal, StoredKey
from spendgate.ledger.keys import KeyService, format_key, hash_secret, parse_key
from spendgate.pipeline.auth import presented_key
from tests.fuzz.conftest import Case, run_fuzz

PEPPER = b"fuzz-pepper-0123456789abcdef"
NOW = 1_800_000_000_000
_EXACT = re.compile(r"(sga?)-([0-9a-f]{12})-([0-9a-f]{48})", re.ASCII)


def _make(
    kind: KeyKind,
    n: int,
    *,
    scopes: frozenset[AdminScope] = frozenset(),
    revoked: bool = False,
    expires: int | None = None,
) -> tuple[str, StoredKey]:
    prefix = f"{n:012x}"
    secret = f"{n * 7919 + 13:048x}"
    record = KeyRecord(
        f"key_{n}",
        prefix,
        kind,
        f"k{n}",
        0,
        KeyPolicy(f"key_{n}", "scope_1"),
        scopes,
        expires,
        NOW - 1 if revoked else None,
    )
    return format_key(kind, prefix, secret), StoredKey(record, hash_secret(secret, PEPPER))


KEYS = {
    "api": _make(KeyKind.API, 1),
    "admin_usage": _make(KeyKind.ADMIN, 2, scopes=frozenset({AdminScope.USAGE_READ})),
    "admin_keys": _make(KeyKind.ADMIN, 3, scopes=frozenset({AdminScope.KEYS_WRITE})),
    "api_revoked": _make(KeyKind.API, 4, revoked=True),
    "api_expired": _make(KeyKind.API, 5, expires=NOW - 5),
    "admin_expired": _make(
        KeyKind.ADMIN, 6, scopes=frozenset({AdminScope.USAGE_READ}), expires=NOW
    ),
}
_BY_PREFIX = {stored.record.prefix: stored for _, stored in KEYS.values()}
_RAW = {name: raw for name, (raw, _) in KEYS.items()}


class StubLedger:
    async def find_key(self, prefix: str) -> StoredKey | None:
        return _BY_PREFIX.get(prefix)


SERVICE = KeyService(
    cast(AdminLedger, StubLedger()), PEPPER, ttl_ms=0
)  # no cache: every call verifies

_LOOKALIKES = [
    "\uff10",
    "\u0660",
    "\u1d41",
    "\u212a",
    "\u00e9",
    "\u200b",
    "\ufeff",
    "\u0131",
    "\u2010",
]


@st.composite
def candidates(draw: st.DrawFn) -> str:
    if draw(st.integers(0, 4)) == 0:
        return draw(st.text(max_size=80))
    raw = _RAW[draw(st.sampled_from(sorted(_RAW)))]
    for _ in range(draw(st.integers(0, 2))):
        at = draw(st.integers(0, len(raw)))
        op = draw(
            st.sampled_from(
                [
                    "set",
                    "case",
                    "tag",
                    "suffix",
                    "prefix",
                    "lookalike",
                    "drop",
                    "dup",
                    "dash",
                    "truncate",
                ]
            )
        )
        if op == "set":
            raw = raw[:at] + draw(st.sampled_from("0123456789abcdefg-gG _\x00")) + raw[at + 1 :]
        elif op == "case":
            raw = raw[:at] + raw[at : at + 1].swapcase() + raw[at + 1 :]
        elif op == "tag":
            raw = (
                raw.replace("sga-", "sg-", 1)
                if raw.startswith("sga-")
                else raw.replace("sg-", "sga-", 1)
            )
        elif op == "suffix":
            raw += draw(st.sampled_from(["\n", " ", "\x00", "\r\n", "a", "-", "\t"]))
        elif op == "prefix":
            raw = draw(st.sampled_from([" ", "\n", "Bearer ", "\ufeff"])) + raw
        elif op == "lookalike":
            raw = raw[:at] + draw(st.sampled_from(_LOOKALIKES)) + raw[at + 1 :]
        elif op == "drop":
            raw = raw[:at] + raw[at + 1 :]
        elif op == "dup":
            raw = raw[:at] + raw[at:]
            raw += raw[-draw(st.integers(1, 20)) :]
        elif op == "dash":
            raw = raw.replace("-", draw(st.sampled_from(["_", "--", ":", ""])), 1)
        else:
            raw = raw[:at]
    return raw


def _expected_parse(text: str) -> tuple[KeyKind, str, str] | None:
    """The documented format, written out independently of the implementation's regex."""
    if not text.isascii():
        return None
    parts = text.split("-")
    if len(parts) != 3 or parts[0] not in ("sg", "sga"):
        return None
    prefix, secret = parts[1], parts[2]
    hexdigits = set("0123456789abcdef")
    if len(prefix) != 12 or len(secret) != 48 or not (set(prefix) | set(secret)) <= hexdigits:
        return None
    return (KeyKind.ADMIN if parts[0] == "sga" else KeyKind.API), prefix, secret


def check_parse(text: str) -> None:
    parsed = parse_key(text)
    want = _expected_parse(text)
    assert (None if parsed is None else (parsed.kind, parsed.prefix, parsed.secret)) == want, repr(
        text
    )
    if parsed is not None:
        assert format_key(parsed.kind, parsed.prefix, parsed.secret) == text
        assert _EXACT.fullmatch(text)


def check_verify(case: tuple[str, AdminScope]) -> None:
    text, required = case

    async def go() -> tuple[Principal | Refusal, Principal | Refusal]:
        return await SERVICE.verify_api(text, NOW), await SERVICE.verify_admin(text, NOW, required)

    api, admin = asyncio.run(go())
    live_api = {_RAW["api"]: "api"}
    live_admin = {
        _RAW["admin_usage"]: AdminScope.USAGE_READ,
        _RAW["admin_keys"]: AdminScope.KEYS_WRITE,
    }
    if text in live_api:
        assert isinstance(api, Principal) and api.key_id == "key_1" and api.kind is KeyKind.API
        assert isinstance(admin, Refusal) and admin.code is ErrorCode.UNAUTHORIZED
    else:
        assert isinstance(api, Refusal) and api.code is ErrorCode.UNAUTHORIZED, (text, api)
    if text in live_admin:
        assert isinstance(admin, Principal) or (
            isinstance(admin, Refusal) and admin.code is ErrorCode.FORBIDDEN
        )
        assert (live_admin[text] is required) == isinstance(admin, Principal)
        assert api is not None and isinstance(api, Refusal)
    elif text not in live_api:
        assert isinstance(admin, Refusal) and admin.code is ErrorCode.UNAUTHORIZED, (text, admin)


_HEADER_VALUES = st.one_of(
    candidates(),
    candidates().map(lambda k: "Bearer " + k),
    candidates().map(lambda k: "bearer   " + k + "  "),
    st.sampled_from(
        [
            "",
            "Bearer",
            "Bearer ",
            "Basic abc",
            "BEARER x",
            "bearer\t" + _RAW["api"],
            " Bearer " + _RAW["api"],
        ]
    ),
    st.text(max_size=40),
)
_HEADERS = st.fixed_dictionaries(
    {},
    optional={
        "authorization": _HEADER_VALUES,
        "x-api-key": _HEADER_VALUES | candidates().map(lambda k: f" {k} "),
    },
)


def check_presented(headers: dict[str, str]) -> None:
    got = presented_key(headers)
    auth = headers.get("authorization", "")
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    sent = headers.get("x-api-key", "")  # a blank header still counts as "sent" next to a bearer
    api = sent.strip()
    if isinstance(got, Refusal):
        assert got.code is ErrorCode.UNAUTHORIZED
        assert (not bearer and not api) or (bearer and sent and bearer != api)
    else:
        assert got and got == got.strip()
        assert got in (bearer, api)
        assert not (bearer and sent) or bearer == api


def test_fuzz_keys() -> None:
    run_fuzz(
        "keys",
        [
            Case("parse_key_exact_format", candidates(), check_parse),
            Case(
                "verify_kinds_and_state",
                st.tuples(candidates(), st.sampled_from(list(AdminScope))),
                check_verify,
            ),
            Case("presented_key", _HEADERS, check_presented),
        ],
    )
