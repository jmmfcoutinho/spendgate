"""spendgate: a small, auditable LLM spend guard."""

import importlib.metadata


def package_version() -> str:
    """The installed distribution's version: one source for `--version`, status and the API."""
    try:
        return importlib.metadata.version("spendgate")
    except importlib.metadata.PackageNotFoundError:  # a source tree that was never installed
        return "unknown"
