"""The `spendgate` command: argument parsing and dispatch.

Exit codes: 0 ok; 1 the server refused the request (invalid_request, not_found, conflict);
2 bad command line; 3 invalid or missing configuration; 4 no admin key or unauthorized or
forbidden; 5 cannot reach the API, ledger unavailable or a server error.

The admin key is read from `SPENDGATE_ADMIN_KEY` only, never from a flag, and never printed.
"""

import argparse
import asyncio
import ipaddress
import json
import os
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from spendgate import package_version
from spendgate.cli import catalog, serve
from spendgate.cli.client import EXIT_AUTH, EXIT_CONFIG, EXIT_OK, Client, CliError
from spendgate.cli.commands import COMMANDS, Command
from spendgate.cli.runtime import open_runtime
from spendgate.config import ConfigError, read_config
from spendgate.gateway.admin import create_admin_app
from spendgate.gateway.admin_http import AdminContext

FALLBACK_URL = "http://127.0.0.1:8080"  # no --url, $SPENDGATE_URL or config: the README's listen
ENV_KEY, ENV_URL, ENV_CONFIG = "SPENDGATE_ADMIN_KEY", "SPENDGATE_URL", "SPENDGATE_CONFIG"
DEFAULT_CONFIG = "spendgate.yaml"
LOCAL_ACTOR = "cli"
_WILDCARD = {"0.0.0.0": "127.0.0.1", "::": "::1"}  # noqa: S104 - a bind-all address, mapped away


type Sub = "argparse._SubParsersAction[argparse.ArgumentParser]"


def _parents() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    """`--config` for every command; `--url`, `--local` and `--json` for admin commands only.

    Defaults are None so `main` can tell what the user typed (echoed in hints) from defaults.
    """
    config = argparse.ArgumentParser(add_help=False)
    config.add_argument(
        "--config", help=f"configuration file (${ENV_CONFIG}, else {DEFAULT_CONFIG})"
    )
    admin = argparse.ArgumentParser(add_help=False, parents=[config])
    admin.add_argument(
        "--url", help=f"admin API URL (${ENV_URL}, else server.admin.listen or server.listen)"
    )
    admin.add_argument(
        "--local", action="store_true", help="use the ledger in --config, not the API"
    )
    admin.add_argument("--json", action="store_true", help="print the API's JSON")
    return config, admin


def _leaf(
    sub: Sub, name: str, common: argparse.ArgumentParser, help_: str
) -> argparse.ArgumentParser:
    return sub.add_parser(name, parents=[common], help=help_)


def _group(top: Sub, name: str, help_: str) -> Sub:
    return top.add_parser(name, help=help_).add_subparsers(dest="action", required=True)


def _keys(top: Sub, common: argparse.ArgumentParser) -> None:
    sub = _group(top, "keys", "virtual keys")
    c = _leaf(sub, "create", common, "create a key; the secret is printed once")
    c.add_argument("--scope", required=True, help="scope id or path, e.g. org:acme/service:api")
    c.add_argument("--name")
    c.add_argument("--models", help="comma-separated allow list (default: every configured model)")
    c.add_argument("--rpm", type=int)
    c.add_argument("--tpm", type=int)
    c.add_argument("--max-concurrent", type=int)
    c.add_argument("--max-output-tokens", type=int)
    c.add_argument("--expires", help="RFC 3339 time")
    u = _leaf(sub, "update", common, "change a key's limits or allowed models")
    u.add_argument("key_id")
    u.add_argument("--models", help="comma-separated allow list (replaces the current one)")
    u.add_argument("--rpm", type=int)
    u.add_argument("--tpm", type=int)
    u.add_argument("--max-concurrent", type=int)
    u.add_argument("--max-output-tokens", type=int)
    ls = _leaf(sub, "list", common, "list keys (never shows secrets)")
    ls.add_argument("--scope")
    ls.add_argument("--include-revoked", action="store_true")
    _leaf(sub, "revoke", common, "revoke a key now").add_argument("key_id")
    r = _leaf(sub, "rotate", common, "issue a replacement; the old key lives on for a grace period")
    r.add_argument("key_id")
    r.add_argument("--grace-seconds", type=int)


def _budgets(top: Sub, common: argparse.ArgumentParser) -> None:
    sub = _group(top, "budgets", "budgets, grants, overrun review")
    s = _leaf(sub, "set", common, "create the budget, or change its cap if it exists")
    s.add_argument("scope", help="scope id or path")
    s.add_argument("--cap", required=True, help="e.g. '50 EUR'")
    s.add_argument("--period", choices=("none", "day", "month"), default="none")
    s.add_argument("--soft-pct", type=int)
    s.add_argument("--model-filter", help="glob over catalog ids, e.g. 'openai/gpt-6-*'")
    s.add_argument("--note")
    _leaf(sub, "list", common, "budgets of a scope with spent and remaining").add_argument("scope")
    g = _leaf(sub, "grant", common, "add temporary extra room")
    g.add_argument("budget_id")
    g.add_argument("--amount", required=True, help="e.g. '10 EUR'")
    g.add_argument("--until", required=True, help="RFC 3339 time, or a duration like 7d")
    g.add_argument("--reason", required=True)
    a = _leaf(sub, "ack-overrun", common, "re-admit calls after a breach overrun was reviewed")
    a.add_argument("budget_id")
    a.add_argument("--reason", required=True)


def build_parser() -> argparse.ArgumentParser:
    config, common = _parents()
    parser = argparse.ArgumentParser(
        prog="spendgate",
        description="spendgate: a hard LLM spend guard",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"spendgate {package_version()}")
    top = parser.add_subparsers(dest="command", required=True)
    s = top.add_parser("serve", parents=[config], help="run the gateway and the admin API")
    s.add_argument("--no-bootstrap", action="store_true", help="skip the config bootstrap section")
    top.add_parser(
        "check-config", parents=[config], help="validate the configuration, start nothing"
    )
    m = top.add_parser("migrate", parents=[config], help="create or upgrade the ledger schema")
    m.add_argument("--bootstrap", action="store_true", help="then apply the bootstrap section")
    _keys(top, common)
    _budgets(top, common)
    kill = _group(top, "kill", "kill switches")
    on = _leaf(kill, "on", common, "engage a kill switch (global unless a target is given)")
    target = on.add_mutually_exclusive_group()
    for name in ("scope", "key", "provider", "model"):
        target.add_argument(f"--{name}")
    on.add_argument("--reason", required=True)
    _leaf(kill, "off", common, "lift a kill switch").add_argument("switch_id")
    u = _leaf(top, "usage", common, "who spent what")
    u.add_argument("--by", help="comma list: scope,service,user,model,provider,day,key,tag,...")
    u.add_argument("--since", help="RFC 3339 time or a duration such as 7d")
    u.add_argument("--until")
    u.add_argument("--scope")
    u.add_argument("--model")
    u.add_argument("--tag")
    u.add_argument("--basis", choices=("charged", "usage"))
    u.add_argument("--limit", type=int)
    _leaf(top, "status", common, "gateway status, kill switches and budget alerts")
    catalog.add_parser(top)
    return parser


def admin_url(args: argparse.Namespace) -> str | int:
    """`--url`, else `$SPENDGATE_URL`, else the listener in `--config`; or the exit code."""
    if args.url is not None:
        return str(args.url)
    if from_env := os.environ.get(ENV_URL):
        return from_env
    path = Path(args.config)
    if not args.config_given and not path.exists():
        return FALLBACK_URL  # a client host without the server's file
    try:
        server = read_config(path).server
    except ConfigError as exc:
        return serve.config_failed(args.config, exc)
    at = serve.parse_listen(server.listen if server.admin is None else server.admin.listen)
    host = _WILDCARD.get(at.host, at.host)  # a bind-all listener is reached on loopback
    if not _loopback(host):
        # The admin key goes in plain HTTP to this address: only to this machine unless asked.
        print(
            f"error: {args.config}: the admin listener {host} is not on loopback; "
            f"pass --url or set {ENV_URL}",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    return f"http://{f'[{host}]' if ':' in host else host}:{at.port}"


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback  # 127.0.0.0/8 and ::1
    except ValueError:
        return False  # any other name


async def _run_admin(args: argparse.Namespace) -> int:
    command = COMMANDS[f"{args.command} {args.action}" if "action" in args else args.command]
    if args.local:
        settings = serve.load_settings(args.config)
        if isinstance(settings, int):
            return settings
        async with open_runtime(settings) as rt:
            ctx = AdminContext(
                rt.ledger, rt.keys, settings, rt.now, rt.now(), None, trusted_actor=LOCAL_ACTOR
            )
            transport = httpx.ASGITransport(app=create_admin_app(ctx, shared_port=False))
            async with httpx.AsyncClient(transport=transport, base_url="http://local") as http:
                return await _emit(command, Client(http), args)
    key = os.environ.get(ENV_KEY, "")
    if not key:
        raise CliError(EXIT_AUTH, f"set {ENV_KEY} to an admin key (sga-...), or use --local")
    url = admin_url(args)
    if isinstance(url, int):
        return url
    async with httpx.AsyncClient(
        base_url=url.rstrip("/"), headers={"Authorization": f"Bearer {key}"}, timeout=30.0
    ) as http:
        return await _emit(command, Client(http), args)


async def _emit(command: Command, client: Client, args: argparse.Namespace) -> int:
    data, text = await command(client, args)
    print(json.dumps(data, indent=2) if args.json else text)
    return EXIT_OK


def _without_userinfo(url: str) -> str:
    """`http://u:p@h:1/x` as `http://h:1/x`: a hint is printed, credentials never are."""
    parts = urlsplit(url)
    return parts._replace(netloc=parts.netloc.rpartition("@")[2]).geturl()


def resolve_defaults(args: argparse.Namespace) -> None:
    """Fill the defaults left as None, keeping the flags the user typed for hints (`a.again`)."""
    typed = ["--local"] if getattr(args, "local", False) else []
    if args.config is not None:
        typed += ["--config", shlex.quote(args.config)]
    if getattr(args, "url", None) is not None:
        typed += ["--url", shlex.quote(_without_userinfo(args.url))]
    args.again = "".join(f" {t}" for t in typed)
    args.config_given = args.config is not None or bool(os.environ.get(ENV_CONFIG))
    args.config = args.config or os.environ.get(ENV_CONFIG) or DEFAULT_CONFIG


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "catalog":  # no config and no admin key: an offline file tool
        return catalog.main(args)
    resolve_defaults(args)
    match args.command:
        case "serve":
            return serve.run_serve(args.config, bootstrap=not args.no_bootstrap)
        case "check-config":
            return serve.run_check_config(args.config)
        case "migrate":
            return serve.run_migrate(args.config, bootstrap=args.bootstrap)
        case _:
            return asyncio.run(_run_admin(args))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return EXIT_OK
