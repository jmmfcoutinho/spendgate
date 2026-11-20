"""Pure parts of key handling: key format, parsing, hashing, specs."""

import re

import pytest

from spendgate.core.policy import KeyPolicy
from spendgate.ledger import keys
from spendgate.ledger.key_types import KeyKind, KeySpec
from spendgate.ledger.keys import MIN_PEPPER_BYTES, format_key, hash_secret, parse_key

PEPPER = b"unit-pepper-0123456789abcdef"


def test_generated_keys_match_the_documented_scanner_regex() -> None:
    for kind, tag in ((KeyKind.API, "sg"), (KeyKind.ADMIN, "sga")):
        raw = format_key(kind, keys.new_prefix(), keys.new_secret())
        assert re.fullmatch(rf"\b{tag}-[0-9a-f]{{12}}-[0-9a-f]{{48}}\b", raw)
    assert r"\bsg-[0-9a-f]{12}-[0-9a-f]{48}\b" in (keys.__doc__ or "")


def test_secret_has_at_least_128_bits_and_is_random() -> None:
    assert keys.SECRET_HEX * 4 >= 128
    assert len({keys.new_secret() for _ in range(50)}) == 50


def test_parse_round_trip_and_kind() -> None:
    prefix, secret = keys.new_prefix(), keys.new_secret()
    for kind in KeyKind:
        parsed = parse_key(format_key(kind, prefix, secret))
        assert parsed is not None and (parsed.kind, parsed.prefix, parsed.secret) == (
            kind,
            prefix,
            secret,
        )
    assert parse_key("nonsense") is None


def test_hash_depends_on_pepper_and_secret_and_is_hex() -> None:
    a = hash_secret("s" * 48, PEPPER)
    assert re.fullmatch(r"[0-9a-f]{64}", a)
    assert a == hash_secret("s" * 48, PEPPER)
    assert a != hash_secret("t" * 48, PEPPER)
    assert a != hash_secret("s" * 48, PEPPER + b"x")


def test_short_pepper_is_refused() -> None:
    with pytest.raises(ValueError, match=str(MIN_PEPPER_BYTES)):
        hash_secret("x", b"short")


def test_key_spec_validation() -> None:
    with pytest.raises(ValueError, match="name"):
        KeySpec("s", "")
    with pytest.raises(ValueError, match="positive"):
        KeySpec("s", "n", rpm=0)
    assert KeyPolicy("k", "s").session_defaults is None
