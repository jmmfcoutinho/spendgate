"""Secret references (`env:NAME`, `file:/path`) and the opaque holder for resolved values.

The YAML never holds a secret, only a pointer to one. A value that does not look like a pointer is
refused without echoing it, so pasting a real key into the file cannot leak it through an error.
A provider credential may also be `adc` (Google) or `entra` (Azure): no secret at all, the
runtime's own cloud identity.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

REF_HELP = (
    "must be a secret reference: 'env:NAME' or 'file:/absolute/path' (never the secret itself)"
)
# Google application default credentials: the workload identity of the host, not a stored secret.
ADC = "adc"
# Microsoft Entra ID through `DefaultAzureCredential`: the managed or workload identity of the host.
ENTRA = "entra"
AMBIENT = frozenset({ADC, ENTRA})
CREDENTIAL_HELP = (
    f"{REF_HELP}, or '{ADC}' for Google application default credentials, or '{ENTRA}' for the "
    "host's Microsoft Entra ID identity"
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


def check_credential_ref(value: object) -> str:
    """A provider credential: a secret reference, `adc` or `entra`. Raises WITHOUT the offending
    text."""
    if value in AMBIENT:
        return str(value)
    try:
        return check_ref(value)
    except ValueError:
        raise ValueError(CREDENTIAL_HELP) from None


_AZURE_ENTRA = frozenset({"azure_openai", "azure_speech"})  # adapters that sign in with Entra ID
# Azure Speech bearer auth is Entra ID only: its own issued tokens last 10 minutes (ADR-0035).
_SPEECH_BEARER = "azure_speech with auth bearer_token signs in with Entra ID: set credential: entra"


def credential_issues(
    provider: str, auth: str, ref: str | None, at: str, where: str
) -> list[tuple[str, str]]:
    """A missing credential (at `at`), a host-identity marker where it does not apply, or a
    credential that would be ignored (at `where`, the key that names it)."""
    if ref is None:
        if auth == "sigv4":
            return []
        kinds = "env:NAME or file:/path"
        if provider in _AZURE_ENTRA and auth == "bearer_token":
            kinds = f"{ENTRA} for the host's Entra ID identity, or {kinds}"
        return [(at, f"auth {auth} needs a credential ({kinds})")]
    if ref == ADC and (provider != "google" or auth != "bearer_token"):
        return [(where, "adc is for Google Vertex AI only (auth bearer_token)")]
    if ref == ENTRA and (provider not in _AZURE_ENTRA or auth != "bearer_token"):
        return [(where, "entra is for azure_openai and azure_speech only (auth bearer_token)")]
    if provider == "azure_speech" and auth == "bearer_token" and ref != ENTRA:
        return [(where, _SPEECH_BEARER)]  # a stored token would be silently ignored
    return []


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
        except UnicodeDecodeError:
            # The decode error holds the file's bytes and quotes one: never chain or show it.
            raise LookupError(f"secret file {rest} is not UTF-8 text") from None
    value = raw.strip()
    if not value:
        raise LookupError(
            f"{'environment variable' if kind == 'env' else 'secret file'} {rest} is empty"
        )
    return Secret(value)
