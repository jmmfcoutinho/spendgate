"""Find provider credentials where only the guard may hold them.

The claim under test: no container but `spendgate` carries a provider credential, the key pepper or
the database URL, in its environment or as a mounted secret. The names to look for are derived from
the code and the shipped files, not hard-coded per provider, so a new provider or secret cannot be
forgotten silently:
  * every provider slug in `spendgate.config.schema.ProviderSlug`;
  * every `file:`/`env:` credential reference in the reference configs;
  * every secret declared in compose.yaml.
"""

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import get_args

import yaml

from spendgate.config.schema import ProviderSlug

GUARD_SERVICE = "spendgate"
# Names a provider's SDK or a deployer would use for the same credential, beyond the slug itself.
# The keys must equal ProviderSlug, so adding a provider fails the tests until this row exists.
PROVIDER_NAME_HINTS: Mapping[str, tuple[str, ...]] = {
    "openai": ("openai",),
    "azure_openai": ("azure",),
    "anthropic": ("anthropic", "claude"),
    "bedrock": ("bedrock", "aws_"),
    "google": ("google", "gemini", "vertex"),
    "azure_speech": ("azure", "speech"),
    "elevenlabs": ("elevenlabs", "xi_api"),
}
# Generic credential words, so `FOO_API_KEY` is caught even for a provider nobody has named yet.
GENERIC_HINTS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "pepper",
    "database_url",
    "dsn",
    "password",
)
# Values that look like keys whatever the variable is called. Virtual `sg-` keys are not here:
# an app is meant to hold one.
KEY_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),  # OpenAI, Anthropic (sk-ant-)
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),  # Google
    re.compile(r"(AKIA|ASIA)[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"[0-9a-f]{32,}"),  # hex pepper or hex-style keys (ElevenLabs, Azure Speech)
    re.compile(r"postgres(ql)?://[^\s:]+:[^\s@]+@"),  # DSN with a password
)
SECRETS_DIR = "/run/secrets"


def _refs(node: object) -> Iterable[str]:
    """Every `file:` / `env:` reference in a parsed config, reduced to the bare name."""
    if isinstance(node, str):
        scheme, _, rest = node.partition(":")
        if scheme in {"file", "env"} and rest:
            yield Path(rest).name
    elif isinstance(node, Mapping):
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


def credential_names(compose_dir: Path) -> frozenset[str]:
    """Lower-case names a leak could carry: secrets, config references, provider hints."""
    names: set[str] = set(GENERIC_HINTS)
    slugs = get_args(ProviderSlug)
    if set(slugs) != set(PROVIDER_NAME_HINTS):
        raise AssertionError(f"PROVIDER_NAME_HINTS is out of date for providers {sorted(slugs)}")
    for hints in PROVIDER_NAME_HINTS.values():
        names.update(hints)
    for config in compose_dir.glob("spendgate.*.yaml"):
        names.update(n.lower() for n in _refs(yaml.safe_load(config.read_text())))
    compose = yaml.safe_load((compose_dir / "compose.yaml").read_text())
    names.update(str(n).lower() for n in compose.get("secrets", {}))
    return frozenset(names)


def find_leaks(
    containers: Iterable[Mapping[str, object]],
    names: Iterable[str],
    secret_values: Iterable[str] = (),
) -> list[str]:
    """Findings for every non-guard container; an empty list means the claim holds."""
    wanted = tuple(names)
    values = tuple(v for v in secret_values if v)
    findings: list[str] = []
    for box in containers:
        config = box["Config"]
        assert isinstance(config, Mapping)
        service = str(config["Labels"].get("com.docker.compose.service", "?"))
        if service == GUARD_SERVICE:
            continue
        where = f"{service} ({str(box['Name']).lstrip('/')})"
        for entry in config.get("Env") or []:
            name, _, value = str(entry).partition("=")
            virtual = value.startswith(
                "sg-"
            )  # an app's own spendgate key (demo: SPENDGATE_API_KEY)
            if not virtual and any(hint in name.lower() for hint in wanted):
                findings.append(f"{where}: environment variable {name} has a credential name")
            if any(s.search(value) for s in KEY_SHAPES) or any(v in value for v in values):
                findings.append(f"{where}: environment variable {name} holds a key-shaped value")
        mounts = box.get("Mounts") or []
        assert isinstance(mounts, list)
        for mount in mounts:
            dest, source = str(mount["Destination"]), str(mount.get("Source", ""))
            # Compose mounts a secret at /run/secrets/<name>; a bind of a credential file may land
            # anywhere, so the file names on both sides are checked too.
            named = {Path(dest).name.lower(), Path(source).name.lower()}
            if dest.startswith(SECRETS_DIR) or any(h in n for h in wanted for n in named):
                findings.append(f"{where}: mounts a credential file ({source} at {dest})")
    return findings
