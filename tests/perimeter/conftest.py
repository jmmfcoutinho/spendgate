"""Fixtures: the reference Compose stack with throw-away secrets and a fake provider upstream.

Nothing here touches deploy/compose/secrets (the operator's real files): secrets, the config and an
overlay that points the compose secrets at them are generated in a temp dir. The stack never
publishes a host port, and `down -v` runs when the module finishes, pass or fail.
"""

import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from tests.perimeter.compose import (
    ADMIN_HOST,
    ADMIN_OVERLAY,
    ADMIN_PORT,
    COMPOSE_DIR,
    Compose,
    docker_ready,
)

FAKE_BASE_URL = "http://127.0.0.1:9000"  # the fake shares spendgate's netns; see compose.test.yaml
TIGHT_SCOPE = "org:acme/project:tight"
APP_SCOPE = "org:acme/project:app"
MAX_BODY_BYTES = 4096


@dataclass(frozen=True, slots=True)
class Stack:
    compose: Compose
    secret_values: tuple[str, ...]  # what the throw-away secret files contain
    workdir: Path


def _write_secrets(workdir: Path, *, postgres: bool) -> dict[str, str]:
    """Key-shaped throw-away values; the DSN and DB password only matter for the Postgres stack."""
    password = secrets.token_hex(8)
    values = {
        "openai_api_key": "sk-" + secrets.token_urlsafe(36),  # pragma: allowlist secret
        "anthropic_api_key": "sk-ant-" + secrets.token_urlsafe(36),  # pragma: allowlist secret
        "spendgate_key_pepper": secrets.token_hex(32),
        "spendgate_database_url": f"postgresql://spendgate:{password}@db:5432/spendgate"
        if postgres
        else "",
        "postgres_password": password if postgres else "",
    }
    sdir = workdir / "secrets"
    sdir.mkdir(mode=0o755)
    for name, value in values.items():
        path = sdir / name
        path.write_text(value)
        path.chmod(0o644)  # the container runs as UID 10001; see deploy/compose/README.md
    overlay = {"secrets": {name: {"file": str(sdir / name)} for name in values}}
    (workdir / "secrets.yaml").write_text(yaml.safe_dump(overlay))
    return values


def _write_config(workdir: Path, *, postgres: bool, admin: bool) -> Path:
    """The shipped reference config, with the upstream pointed at the fake and tight test limits.

    With `admin`, the admin listener moves to the `admin` network as deploy/compose/README.md says.
    """
    shipped = COMPOSE_DIR / f"spendgate.{'postgres' if postgres else 'sqlite'}.yaml"
    config = yaml.safe_load(shipped.read_text())
    for provider in config["providers"].values():
        provider["base_url"] = FAKE_BASE_URL
    config["server"]["max_body_bytes"] = MAX_BODY_BYTES
    config["server"]["max_json_body_bytes"] = MAX_BODY_BYTES
    if admin:
        config["server"]["admin"] = {"listen": f"{ADMIN_HOST}:{ADMIN_PORT}"}
    bootstrap = config["bootstrap"]
    bootstrap["scopes"].append(TIGHT_SCOPE)
    # Below the worst-case hold of any call, so every request on this scope is over budget.
    bootstrap["budgets"].append(
        {"id": "tight", "scope": TIGHT_SCOPE, "period": "day", "cap": "0.000001 EUR"}
    )
    path = workdir / "spendgate.test.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def _make(workdir: Path, *, postgres: bool, admin: bool) -> Stack:
    values = _write_secrets(workdir, postgres=postgres)
    config = _write_config(workdir, postgres=postgres, admin=admin)
    env = {"SPENDGATE_TEST_CONFIG": str(config)}
    if postgres:
        env |= {"SPENDGATE_VARIANT": "full", "SPENDGATE_LEDGER": "postgres"}
    compose = Compose(
        files=[
            COMPOSE_DIR / "compose.yaml",
            COMPOSE_DIR / "compose.test.yaml",
            *([ADMIN_OVERLAY] if admin else []),
            workdir / "secrets.yaml",
        ],
        env=env,
        profiles=("postgres",) if postgres else (),
    )
    return Stack(compose, tuple(v for v in values.values() if v), workdir)


def _running(
    workdir: Path, *, postgres: bool, markexpr: str, admin: bool = False
) -> Iterator[Stack]:
    # `make test` also collects docker-marked tests; they build images, so they run only on request.
    if "docker" not in markexpr.replace("not docker", ""):
        pytest.skip("run with -m docker to start the Compose stack")
    if not docker_ready():
        pytest.skip("docker (with the compose plugin) is not available")
    stack = _make(workdir, postgres=postgres, admin=admin)
    try:
        stack.compose.start_stack(postgres=postgres)
        yield stack
    finally:
        stack.compose.down()


@pytest.fixture(scope="module")
def sqlite_stack(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[Stack]:
    markexpr = str(request.config.getoption("-m"))
    yield from _running(tmp_path_factory.mktemp("sg-sqlite"), postgres=False, markexpr=markexpr)


@pytest.fixture(scope="module")
def postgres_stack(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[Stack]:
    markexpr = str(request.config.getoption("-m"))
    yield from _running(tmp_path_factory.mktemp("sg-postgres"), postgres=True, markexpr=markexpr)


@pytest.fixture(scope="module")
def admin_stack(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[Stack]:
    """The SQLite stack plus compose.admin.yaml, the admin listener on the `admin` network."""
    markexpr = str(request.config.getoption("-m"))
    workdir = tmp_path_factory.mktemp("sg-admin")
    yield from _running(workdir, postgres=False, markexpr=markexpr, admin=True)
