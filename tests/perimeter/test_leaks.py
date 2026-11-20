"""The leak checker on hand-made `docker inspect` data: no Docker needed."""

from collections.abc import Mapping
from pathlib import Path

from tests.perimeter.leaks import credential_names, find_leaks

COMPOSE_DIR = Path(__file__).resolve().parents[2] / "deploy" / "compose"


def _box(service: str, env: list[str], mounts: list[str] | None = None) -> Mapping[str, object]:
    return {
        "Name": f"/p-{service}-1",
        "Config": {"Labels": {"com.docker.compose.service": service}, "Env": env},
        "Mounts": [{"Destination": d, "Source": "/host/x"} for d in mounts or []],
    }


def test_names_cover_every_provider_and_the_shipped_secrets() -> None:
    names = credential_names(COMPOSE_DIR)
    assert {"elevenlabs", "bedrock", "spendgate_key_pepper", "spendgate_database_url"} <= names


def test_clean_apps_pass_and_the_guard_is_exempt() -> None:
    names = credential_names(COMPOSE_DIR)
    virtual = "SPENDGATE_API_KEY=sg-abcdef123456-" + "1" * 8  # pragma: allowlist secret
    boxes = [
        _box("app", ["PATH=/usr/bin", virtual]),
        _box("spendgate", ["OPENAI_API_KEY=sk-" + "a" * 30], ["/run/secrets/openai_api_key"]),
    ]
    assert find_leaks(boxes, names) == []


def test_each_kind_of_leak_is_reported() -> None:
    names = credential_names(COMPOSE_DIR)
    boxes = [
        _box("a", ["ELEVENLABS_API_KEY=x"]),
        _box("b", ["NOTES=sk-" + "a" * 30]),
        _box("c", [], ["/run/secrets/anything"]),
        _box("d", ["X=pw"]),
    ]
    findings = find_leaks(boxes, names, secret_values=["pw"])
    assert [f[0] for f in findings] == ["a", "b", "c", "d"]
