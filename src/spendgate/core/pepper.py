"""The key pepper's minimum length, shared by the config check and the key hasher.

The pepper is the HMAC key for stored key secrets (threat model C-01). The configuration refuses
a shorter one as a config error before anything starts; the ledger refuses it again on use.
"""

from typing import Final

MIN_PEPPER_BYTES: Final = 16  # counted on the UTF-8 encoding, after the secret is stripped
