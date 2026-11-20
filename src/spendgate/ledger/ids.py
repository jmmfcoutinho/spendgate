"""Lowercase, time-ordered ids (ULID layout: 48-bit millisecond time + 80 random bits)."""

import secrets
import time

# Crockford base32, lowercase.
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # pragma: allowlist secret


def new_id() -> str:
    value = (time.time_ns() // 1_000_000) << 80 | secrets.randbits(80)
    return "".join(_ALPHABET[(value >> shift) & 31] for shift in range(125, -1, -5))
