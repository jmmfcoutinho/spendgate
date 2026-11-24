"""Database tokens for `ledger.backend.auth` and DSN rotation: fake identities, no network."""

import asyncio
import importlib.util
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

import psycopg
import pytest

from spendgate.ledger.port import LedgerUnavailable
from spendgate.ledger.postgres import PostgresLedger, same_but_password
from spendgate.ledger.postgres_auth import (
    CLOUDSQL_SCOPE,
    RDS_TOKEN_SECONDS,
    REFRESH_MARGIN_SECONDS,
    UNKNOWN_EXPIRY_SECONDS,
    DbTokens,
    cloudsql_fetch,
    connection_class,
    db_tokens,
    entra_fetch,
    rds_fetch,
)

# Also `postgres`: these need no server, but `make test-postgres` gates `ledger/postgres*` coverage
# on the Postgres run alone, and the token code is exercised only here.
pytestmark = [pytest.mark.anyio, pytest.mark.postgres]

DSN = "postgresql://spendgate@db.example.test:6432/spendgate?sslmode=verify-full"
NOW = 1_800_000_000.0
TOKEN = "fake-db-token-0000"  # noqa: S105  # pragma: allowlist secret


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


class Fetches:
    """A fake token generator: numbered tokens, each valid for `lifetime` seconds."""

    def __init__(self, clock: Clock, lifetime: float = 3600) -> None:
        self.clock, self.lifetime, self.count = clock, lifetime, 0

    def __call__(self) -> tuple[str, float]:
        self.count += 1
        return f"{TOKEN}-{self.count}", self.clock() + self.lifetime


async def test_a_token_is_reused_until_it_nears_expiry_then_fetched_again() -> None:
    clock = Clock()
    fetch = Fetches(clock)
    tokens = DbTokens("entra", fetch, clock=clock)
    assert await tokens.password() == f"{TOKEN}-1"
    clock.now += 3600 - REFRESH_MARGIN_SECONDS - 1
    assert await tokens.password() == f"{TOKEN}-1" and fetch.count == 1
    clock.now += 1  # inside the margin: replaced before any connection logs in with it
    assert await tokens.password() == f"{TOKEN}-2" and fetch.count == 2


async def test_concurrent_connections_share_one_fetch() -> None:
    clock = Clock()
    fetch = Fetches(clock)
    tokens = DbTokens("rds_iam", fetch, clock=clock)
    got = await asyncio.gather(*(tokens.password() for _ in range(8)))
    assert set(got) == {f"{TOKEN}-1"} and fetch.count == 1


async def test_a_failed_fetch_is_ledger_unavailable_naming_only_the_class() -> None:
    def broken() -> tuple[str, float]:
        raise RuntimeError("https://db/?X-Amz-Signature=secret-ish")

    with pytest.raises(LedgerUnavailable) as info:
        await DbTokens("rds_iam", broken).password()
    assert "RuntimeError" in str(info.value) and "Signature" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None
    with pytest.raises(LedgerUnavailable, match="empty token"):
        await DbTokens("entra", lambda: ("", NOW + 3600), clock=Clock()).password()


async def test_the_token_never_shows_in_the_repr() -> None:
    clock = Clock()
    tokens = DbTokens("entra", Fetches(clock), clock=clock)
    await tokens.password()
    assert TOKEN not in repr(tokens) and repr(tokens) == "DbTokens(entra)"


class FakeRds:
    def __init__(self) -> None:
        self.asked: list[tuple[str, int, str]] = []

    def generate_db_auth_token(self, DBHostname: str, Port: int, DBUsername: str) -> str:
        self.asked.append((DBHostname, Port, DBUsername))
        return TOKEN


def test_rds_tokens_are_signed_for_the_dsn_host_port_and_user() -> None:
    client = FakeRds()
    token, expires = rds_fetch(DSN, "eu-west-1", client=client, clock=Clock())()
    assert token == TOKEN and expires == NOW + RDS_TOKEN_SECONDS
    assert client.asked == [("db.example.test", 6432, "spendgate")]


@pytest.mark.parametrize(
    ("dsn", "problem"),
    [
        ("postgresql://spendgate:pw@db/x", "must not carry a password"),  # pragma: allowlist secret
        ("postgresql://db/x", "must name the database user"),
        ("host=/run/postgresql user=spendgate dbname=x", "database host name"),
    ],
)
def test_token_auth_refuses_a_dsn_that_cannot_use_it(dsn: str, problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        rds_fetch(dsn, None, client=FakeRds())


@dataclass(frozen=True)
class AccessToken:
    token: str
    expires_on: int


def test_entra_tokens_come_from_the_credential_for_the_configured_scope() -> None:
    asked: list[str] = []

    class Credential:
        def get_token(self, *scopes: str) -> AccessToken:
            asked.extend(scopes)
            return AccessToken(TOKEN, int(NOW) + 3600)

    fetch = entra_fetch(DSN, "https://example.test/.default", credential=Credential())
    assert fetch() == (TOKEN, NOW + 3600) and asked == ["https://example.test/.default"]


class GoogleCreds:
    def __init__(self, expiry: datetime | None) -> None:
        self.token: str | None = None
        self.expiry = expiry
        self.refreshes = 0

    def refresh(self, request: object) -> None:
        self.refreshes += 1
        self.token = TOKEN


def test_cloud_sql_tokens_are_refreshed_google_access_tokens() -> None:
    expiry = datetime.fromtimestamp(NOW + 3600, UTC).replace(tzinfo=None)  # google-auth: naive UTC
    creds = GoogleCreds(expiry)
    assert cloudsql_fetch(DSN, credentials=creds)() == (TOKEN, NOW + 3600)
    no_expiry = GoogleCreds(None)
    token, expires = cloudsql_fetch(DSN, credentials=no_expiry, clock=Clock())()
    assert token == TOKEN and expires == NOW + UNKNOWN_EXPIRY_SECONDS
    assert CLOUDSQL_SCOPE.endswith("sqlservice.login")


def test_cloud_sql_uses_application_default_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    import google.auth  # noqa: PLC0415 - the google extra is installed for tests

    creds = GoogleCreds(None)
    monkeypatch.setattr(google.auth, "default", lambda scopes: (creds, "p"))
    assert cloudsql_fetch(DSN)()[0] == TOKEN and creds.refreshes == 1


def test_db_tokens_builds_each_kind_and_refuses_others() -> None:
    for kind in ("rds_iam", "entra", "cloudsql_iam"):
        assert repr(db_tokens(kind, DSN, region="eu-west-1", scope="s")) == f"DbTokens({kind})"
    with pytest.raises(ValueError, match="unknown"):
        db_tokens("kerberos", DSN)


# --- the ledger side: a password per connection, DSN rotation ------------------------------------


def test_only_a_password_change_is_a_rotation() -> None:
    old = "postgresql://u:old@db:5432/spend?sslmode=verify-full"  # pragma: allowlist secret
    assert same_but_password(old, old.replace(":old@", ":new@"))
    assert not same_but_password(old, old.replace("@db:", "@other:"))
    assert not same_but_password(old, old.replace("/spend", "/other"))


def test_rotate_dsn_takes_a_new_password_and_refuses_a_new_target() -> None:
    old = "postgresql://u:old@db/spend"  # pragma: allowlist secret
    ledger = PostgresLedger(old, "USD", lambda: "id")
    assert ledger.rotate_dsn("postgresql://u:new@db/spend") is True  # pragma: allowlist secret
    assert ledger.rotate_dsn("postgresql://u:new@db/spend") is False  # pragma: allowlist secret
    with pytest.raises(ValueError, match="restart"):
        ledger.rotate_dsn("postgresql://u:new@elsewhere/spend")  # pragma: allowlist secret


class FakeLogin:
    def __init__(self) -> None:
        self.asked = self.invalidated = 0

    async def password(self) -> str:
        self.asked += 1
        return TOKEN

    def invalidate(self) -> None:
        self.invalidated += 1


async def test_every_new_connection_asks_for_the_current_password() -> None:
    login = FakeLogin()
    ledger = PostgresLedger(DSN, "USD", lambda: "id", login=login)
    first = await ledger._connect_kwargs()
    second = await ledger._connect_kwargs()
    assert first["password"] == second["password"] == TOKEN and login.asked == 2
    assert first["autocommit"] is True and first["keepalives"] == "1"
    plain = await PostgresLedger(DSN, "USD", lambda: "id")._connect_kwargs()
    assert "password" not in plain


# --- a rejected token is not reused; a malformed DSN is never quoted ----------------------------

MALFORMED = "postgresql://spendgate:pw-fragment-0000@[db/spend"  # pragma: allowlist secret


async def test_an_invalidated_token_is_fetched_again_before_it_expires() -> None:
    clock = Clock()
    fetch = Fetches(clock)
    tokens = DbTokens("rds_iam", fetch, clock=clock)
    assert await tokens.password() == f"{TOKEN}-1"
    tokens.invalidate()  # the server refused it: do not wait out its lifetime
    assert await tokens.password() == f"{TOKEN}-2" and fetch.count == 2


async def test_a_cancelled_caller_does_not_start_a_second_concurrent_fetch() -> None:
    started, release = threading.Event(), threading.Event()
    clock, calls = Clock(), [0]

    def slow() -> tuple[str, float]:
        calls[0] += 1
        started.set()
        assert release.wait(timeout=10)
        return TOKEN, clock() + 3600

    tokens = DbTokens("entra", slow, clock=clock)
    first = asyncio.ensure_future(tokens.password())
    assert await asyncio.to_thread(started.wait, 10)
    first.cancel()  # the caller holding the fetch gives up; the thread keeps running
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.ensure_future(tokens.password())
    release.set()
    assert await second == TOKEN and calls[0] == 1


async def test_a_failed_connect_forgets_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    login = FakeLogin()

    async def refused(*args: object, **kwargs: object) -> NoReturn:
        raise psycopg.OperationalError("password authentication failed")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", classmethod(refused))
    with pytest.raises(psycopg.OperationalError):
        await connection_class(login).connect("host=db.example.test")
    assert login.invalidated == 1


# libpq quotes the host it could not resolve; with an unencoded `@` in the password, that host
# holds the rest of the password.
UNENCODED = (
    "postgresql://spendgate:pw@fragment-0000@127.0.0.1:5432/spend"  # pragma: allowlist secret
)


async def _libpq_refuses(*args: object, **kwargs: object) -> NoReturn:
    raise psycopg.OperationalError('failed to resolve host "fragment-0000@127.0.0.1"')


async def test_a_failed_connect_never_quotes_the_connection_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", classmethod(_libpq_refuses))
    with pytest.raises(psycopg.OperationalError) as info:
        await connection_class(None).connect(UNENCODED)
    text = str(info.value)
    assert text.startswith("cannot connect to the ledger database (OperationalError)")
    assert "percent-encode '@' in the password as %40" in text and "fragment" not in text
    assert info.value.__cause__ is None and info.value.__context__ is None
    with pytest.raises(LedgerUnavailable) as opened:
        await PostgresLedger.open(UNENCODED, currency="USD")
    assert "fragment" not in str(opened.value) and "%40" in str(opened.value)
    assert opened.value.__cause__ is None and opened.value.__context__ is None


@pytest.mark.parametrize(
    ("kind", "module", "extra"),
    [
        ("rds_iam", "botocore", "bedrock"),
        ("entra", "azure.identity", "azure"),
        ("cloudsql_iam", "requests", "google"),
    ],
)
def test_a_kind_whose_extra_is_missing_is_refused_naming_the_extra(
    monkeypatch: pytest.MonkeyPatch, kind: str, module: str, extra: str
) -> None:
    real = importlib.util.find_spec

    def find_spec(name: str) -> object:
        if name == module:
            raise ModuleNotFoundError(name)  # as when a parent package is missing
        return real(name)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    with pytest.raises(ValueError, match=rf"needs the optional extra spendgate\[{extra}\]"):
        db_tokens(kind, DSN)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with pytest.raises(ValueError, match=module):
        db_tokens(kind, DSN)


def test_a_malformed_dsn_is_refused_without_quoting_it() -> None:
    with pytest.raises(ValueError, match="not a valid connection string") as info:
        rds_fetch(MALFORMED, None, client=FakeRds())
    assert "pw-fragment" not in str(info.value) and info.value.__context__ is None
    with pytest.raises(ValueError, match="not a valid connection string") as info:
        same_but_password("postgresql://u:old@db/spend", MALFORMED)  # pragma: allowlist secret
    assert "pw-fragment" not in str(info.value) and info.value.__context__ is None
    with pytest.raises(ValueError, match="not a valid connection string"):
        entra_fetch("postgresql://spendgate@db:notaport/spend", "s")


async def test_open_with_a_malformed_dsn_never_quotes_it() -> None:
    with pytest.raises(LedgerUnavailable) as info:
        await PostgresLedger.open(MALFORMED, currency="USD")
    assert "not a valid connection string" in str(info.value)
    assert "pw-fragment" not in str(info.value) and info.value.__context__ is None
