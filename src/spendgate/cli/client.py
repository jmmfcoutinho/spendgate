"""The CLI's one HTTP client: a real admin API over TCP, or the admin app in-process (`--local`)."""

import httpx

type Json = dict[str, object]

EXIT_OK = 0
EXIT_REFUSED = 1  # the server understood and said no: invalid_request, not_found, conflict
EXIT_USAGE = 2  # argparse: bad command line
EXIT_CONFIG = 3  # the configuration file is invalid or missing
EXIT_AUTH = 4  # no admin key, or unauthorized / forbidden
EXIT_UNAVAILABLE = 5  # cannot reach the API, ledger unavailable, or a server error
_AUTH_STATUS = (401, 403)
_SERVER_ERROR = 500


class CliError(Exception):
    """Prints `message` on stderr and exits with `code`."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _exit_code(status: int) -> int:
    if status in _AUTH_STATUS:
        return EXIT_AUTH
    return EXIT_UNAVAILABLE if status >= _SERVER_ERROR else EXIT_REFUSED


class Client:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def call(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: Json | None = None,
    ) -> Json:
        """The decoded JSON answer (empty for 204); any failure raises `CliError`."""
        try:
            r = await self._http.request(method, path, params=query, json=body)
        except httpx.TransportError:
            raise CliError(EXIT_UNAVAILABLE, "cannot reach the admin API") from None
        if r.status_code < 400:  # noqa: PLR2004
            decoded: Json = r.json() if r.content else {}
            return decoded
        try:
            err = r.json()["error"]
            text = f"{err['code']}: {err['message']}" + (
                f" (param {err['param']})" if err.get("param") else ""
            )
        except (ValueError, KeyError, TypeError):
            text = f"HTTP {r.status_code}"
        raise CliError(_exit_code(r.status_code), text)
