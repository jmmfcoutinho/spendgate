"""Secret references (`env:NAME`, `file:/path`) and the opaque holder for resolved values.

The YAML never holds a secret, only a pointer to one. A value that does not look like a pointer is
refused without echoing it, so pasting a real key into the file cannot leak it through an error.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

REF_HELP = (
    "must be a secret reference: 'env:NAME' or 'file:/absolute/path' (never the secret itself)"
)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True, slots=True)
class Secret:
    """A resolved secret; repr, str and logging show nothing."""

    _value: str = field(repr=False)

    def reveal(self) -> str:
        """The raw value, for the one place that must send it (an adapter header, the HMAC key)."""
        return self._value

    def __repr__(self) -> str:
        return "Secret(****)"

    __str__ = __repr__


def check_ref(value: object) -> str:
    """Validate the shape of a reference. Raises ValueError WITHOUT the offending text."""
    if not isinstance(value, str):
        raise ValueError(REF_HELP)
    kind, _, rest = value.partition(":")
    if kind == "env" and _ENV_NAME.fullmatch(rest):
        return value
    if kind == "file" and rest.startswith("/") and len(rest) > 1:
        return value
    raise ValueError(REF_HELP)


def resolve(ref: str, env: Mapping[str, str]) -> Secret:
    """Read the secret a validated reference points to; LookupError names the source only."""
    kind, _, rest = ref.partition(":")
    if kind == "env":
        raw = env.get(rest)
        if raw is None:
            raise LookupError(f"environment variable {rest} is not set")
    else:
        try:
            raw = Path(rest).read_text(encoding="utf-8")
        except OSError as exc:
            raise LookupError(f"secret file {rest} cannot be read ({type(exc).__name__})") from exc
    value = raw.strip()
    if not value:
        raise LookupError(
            f"{'environment variable' if kind == 'env' else 'secret file'} {rest} is empty"
        )
    return Secret(value)
