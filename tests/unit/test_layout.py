"""Guards the package tree against architecture.md section 2 and imports every module."""

import importlib
import pkgutil

import spendgate

PACKAGES = {
    "core",
    "ledger",
    "providers",
    "config",
    "client",
    "pipeline",
    "gateway",
    "local",
    "cli",
}
PROVIDERS = {
    "openai",
    "azure_openai",
    "anthropic",
    "bedrock",
    "google",
    "azure_speech",
    "elevenlabs",
}


def _walk() -> list[pkgutil.ModuleInfo]:
    return list(pkgutil.walk_packages(spendgate.__path__, "spendgate."))


def test_top_level_packages_match_the_architecture() -> None:
    top = {m.name.split(".")[1] for m in _walk() if m.ispkg and m.name.count(".") == 1}
    assert top == PACKAGES


def test_every_provider_adapter_exists_with_a_port() -> None:
    names = {m.name.rsplit(".", 1)[1] for m in _walk() if m.name.startswith("spendgate.providers.")}
    assert PROVIDERS | {"port", "sse"} <= names
    # Helper modules belong to one adapter and say which by their prefix (bedrock_stream, ...);
    # `sse` is the one provider-neutral codec every SSE adapter shares.
    helpers = names - PROVIDERS - {"port", "sse"}
    orphans = {h for h in helpers if not any(h.startswith(f"{p}_") for p in PROVIDERS)}
    assert not orphans


def test_every_module_imports_cleanly() -> None:
    for module in _walk():
        importlib.import_module(module.name)
