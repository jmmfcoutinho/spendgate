"""Short-lived Postgres passwords from a cloud identity: RDS IAM, Entra ID, Cloud SQL IAM.

With `ledger.backend.auth` the DSN carries no password: every new pooled connection asks
`DbTokens.password()` for one. A token lives 15 minutes (RDS) to an hour (Entra, Google) and is
needed only to log in, so one token serves every connection opened until shortly before it
expires. Getting one blocks (a signature over credentials the AWS chain may first fetch, or an
HTTP call to Entra or Google), so it runs in a worker thread, one fetch at a time.

Each identity library is an optional extra, imported only when its kind is configured: `rds_iam`
uses botocore (the `bedrock` extra), `entra` azure-identity (`azure`), `cloudsql_iam` google-auth
(`google`). A failure is `LedgerUnavailable`, naming the kind and the error class only.
"""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC
from typing import Any, Final, Protocol, Self, cast

import psycopg
from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import DictRow

from spendgate.ledger.port import LedgerUnavailable

REFRESH_MARGIN_SECONDS: Final = 300  # a token this close to expiry is replaced before use
RDS_TOKEN_SECONDS: Final = 900  # the lifetime AWS documents for an RDS auth token
UNKNOWN_EXPIRY_SECONDS: Final = REFRESH_MARGIN_SECONDS + 60  # no expiry given: reuse ~1 minute
CLOUDSQL_SCOPE: Final = "https://www.googleapis.com/auth/sqlservice.login"
KINDS: Final = ("rds_iam", "entra", "cloudsql_iam")

type Clock = Callable[[], float]  # Unix seconds
type Fetch = Callable[[], tuple[str, float]]  # blocking: (token, expires at in Unix seconds)
type Conn = AsyncConnection[DictRow]


class Login(Protocol):
    """What the ledger asks of a token source: a password per login, and to forget a bad one."""

    async def password(self) -> str: ...

    def invalidate(self) -> None: ...


class DbTokens:
    """The password for the next new connection: a cached token, fetched off the event loop."""

    def __init__(self, kind: str, fetch: Fetch, *, clock: Clock = time.time) -> None:
        self._kind, self._fetch, self._clock = kind, fetch, clock
        self._token: tuple[str, float] | None = None
        self._fetching: asyncio.Task[str] | None = None

    def _fresh(self) -> str | None:
        token = self._token
        if token is None or token[1] - self._clock() <= REFRESH_MARGIN_SECONDS:
            return None
        return token[0]

    async def password(self) -> str:
        """A token with more than `REFRESH_MARGIN_SECONDS` left; fetched when there is none."""
        if (ready := self._fresh()) is not None:
            return ready
        loop = asyncio.get_running_loop()
        task = self._fetching
        # One fetch at a time: callers share the task in flight. A task belongs to one loop, and
        # `open` may run on another loop than the pool does.
        if task is None or task.done() or task.get_loop() is not loop:
            task = self._fetching = loop.create_task(self._fetch_one())
            task.add_done_callback(_retrieve)
        # Shielded: a cancelled caller leaves the fetch running for the others, instead of
        # letting the next caller start a second one beside the thread still at work.
        return await asyncio.shield(task)

    async def _fetch_one(self) -> str:
        try:
            token, expires = await asyncio.to_thread(self._fetch)
        except Exception as exc:  # the message may quote a signed URL: the class only,
            # and no chained cause, which a traceback at start would print in full
            raise LedgerUnavailable(
                f"cannot get a {self._kind} database token ({type(exc).__name__})"
            ) from None
        if not token:
            raise LedgerUnavailable(f"the {self._kind} identity returned an empty token")
        self._token = (token, expires)
        return token

    def invalidate(self) -> None:
        """Forget the cached token: a login failed, maybe because the server rejected it."""
        self._token = None

    def __repr__(self) -> str:  # the token lives only in `_token`; never print it
        return f"DbTokens({self._kind})"


def _retrieve(task: "asyncio.Task[str]") -> None:
    # Every caller may have been cancelled: read the outcome so asyncio does not log it as lost.
    if not task.cancelled():
        task.exception()


def connection_class(login: Login | None) -> type[Conn]:
    """The connection class for `login`: a failed connect forgets its cached token.

    libpq reports a rejected login (SQLSTATE 28P01 or 28000) as an `OperationalError` that often
    carries no SQLSTATE, so any failed connect drops the token; the next login fetches a new one.
    """
    if login is None:
        return AsyncConnection
    forget = login.invalidate

    class TokenConnection(AsyncConnection[DictRow]):
        @classmethod
        async def connect(cls, conninfo: str = "", **kwargs: Any) -> Self:  # noqa: ANN401
            try:
                return await super().connect(conninfo, **kwargs)
            except psycopg.OperationalError:
                forget()
                raise

    return TokenConnection


class RdsClient(Protocol):
    def generate_db_auth_token(  # botocore's own parameter names
        self, DBHostname: str, Port: int, DBUsername: str
    ) -> str: ...


class TokenCredential(Protocol):  # azure.core.credentials.TokenCredential, as far as used
    def get_token(self, *scopes: str) -> Any: ...  # noqa: ANN401 - an AccessToken


class GoogleCredentials(Protocol):  # google.auth.credentials.Credentials, as far as used
    token: str | None
    expiry: Any  # a naive UTC datetime, or None

    def refresh(self, request: object) -> None: ...


def _missing(extra: str) -> RuntimeError:
    return RuntimeError(f"this ledger auth kind needs the optional extra: spendgate[{extra}]")


def _login(dsn: str) -> tuple[str, int, str]:
    """Host, port and user of the DSN, which must name the user and carry no password."""
    try:
        params = conninfo_to_dict(dsn)
        port = int(str(params.get("port") or 5432))
    except (psycopg.Error, ValueError):  # psycopg's message may quote the DSN, password included
        raise ValueError("ledger.backend.dsn is not a valid connection string") from None
    if params.get("password"):
        raise ValueError(
            "ledger.backend.dsn must not carry a password when ledger.backend.auth is set"
        )
    user = str(params.get("user") or "")
    if not user:
        raise ValueError("ledger.backend.dsn must name the database user for token auth")
    return str(params.get("host") or ""), port, user


def rds_fetch(
    dsn: str, region: str | None, *, client: RdsClient | None = None, clock: Clock = time.time
) -> Fetch:
    """An RDS IAM token: a SigV4 presigned URL, signed locally with the AWS credential chain."""
    host, port, user = _login(dsn)
    if not host or host.startswith("/"):
        raise ValueError("rds_iam needs the database host name in ledger.backend.dsn")
    made: list[RdsClient] = [client] if client is not None else []

    def fetch() -> tuple[str, float]:
        if not made:
            try:
                import botocore.session  # noqa: PLC0415 - optional extra, rds_iam only
            except ImportError as missing:  # pragma: no cover - the extra is installed in CI
                raise _missing("bedrock") from missing
            client = botocore.session.get_session().create_client("rds", region_name=region)
            made.append(cast("RdsClient", client))  # the method is added at client creation
        token = made[0].generate_db_auth_token(DBHostname=host, Port=port, DBUsername=user)
        return token, clock() + RDS_TOKEN_SECONDS

    return fetch


def entra_fetch(dsn: str, scope: str, *, credential: TokenCredential | None = None) -> Fetch:
    """An Entra ID access token for Azure Database for PostgreSQL (`DefaultAzureCredential`)."""
    _login(dsn)
    made: list[TokenCredential] = [credential] if credential is not None else []

    def fetch() -> tuple[str, float]:
        if not made:
            try:
                from azure.identity import DefaultAzureCredential  # noqa: PLC0415 - optional extra
            except ImportError as missing:  # pragma: no cover - the extra is installed in CI
                raise _missing("azure") from missing
            made.append(DefaultAzureCredential())
        got = made[0].get_token(scope)
        return str(got.token), float(got.expires_on)

    return fetch


def _default_google() -> GoogleCredentials:
    try:
        import google.auth  # noqa: PLC0415 - optional extra, cloudsql_iam only
    except ImportError as missing:  # pragma: no cover - the extra is installed in CI
        raise _missing("google") from missing
    creds, _ = google.auth.default(scopes=[CLOUDSQL_SCOPE])
    found: GoogleCredentials = creds
    return found


def cloudsql_fetch(
    dsn: str, *, credentials: GoogleCredentials | None = None, clock: Clock = time.time
) -> Fetch:
    """A Google OAuth access token for Cloud SQL IAM database sign-in (application default)."""
    _login(dsn)
    made: list[GoogleCredentials] = [credentials] if credentials is not None else []

    def fetch() -> tuple[str, float]:
        if not made:
            made.append(_default_google())
        from google.auth.transport.requests import Request  # noqa: PLC0415 - optional extra

        made[0].refresh(Request())  # a refresh is a new token: the cache above makes it rare
        expiry = made[0].expiry
        if expiry is None:
            return made[0].token or "", clock() + UNKNOWN_EXPIRY_SECONDS
        return made[0].token or "", float(expiry.replace(tzinfo=UTC).timestamp())

    return fetch


def db_tokens(kind: str, dsn: str, *, region: str | None = None, scope: str = "") -> DbTokens:
    """The token source for `ledger.backend.auth`; ValueError when the DSN does not fit it."""
    if kind == "rds_iam":
        return DbTokens(kind, rds_fetch(dsn, region))
    if kind == "entra":
        return DbTokens(kind, entra_fetch(dsn, scope))
    if kind == "cloudsql_iam":
        return DbTokens(kind, cloudsql_fetch(dsn))
    raise ValueError(f"unknown ledger auth kind {kind!r}")
