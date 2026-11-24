"""`spendgate catalog import`: draft an override file from LiteLLM's community price file.

An operator action at a desk, never part of serving: the proxy and library mode read only local
catalog files (ADR-0014). The file comes from one pinned commit of BerriAI/litellm over https on
raw.githubusercontent.com, or from a local copy. The output is a new override file of
`verified: false` entries for models the bundled catalog does not carry; an existing file is
never overwritten without `--force`.

Exit codes: 0 written; 2 bad command line, or the output exists; 3 the input is not LiteLLM's
JSON, or nothing could be drafted; 5 the download failed.
"""

import argparse
import hashlib
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

from spendgate.cli.client import EXIT_CONFIG, EXIT_OK, EXIT_UNAVAILABLE, EXIT_USAGE, CliError
from spendgate.config.yamlio import load_yaml_bytes
from spendgate.core import litellm
from spendgate.core.catalog import PROVIDERS, CatalogError, parse

TIMEOUT_SECONDS = 60.0
_REPO_URL = f"https://github.com/{litellm.REPO}"  # source_url of a copy whose commit is unnamed

type Fetch = Callable[[str], bytes]


def add_parser(top: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    sub = top.add_parser("catalog", help="catalog tools (nothing here runs at serve time)")
    actions = sub.add_subparsers(dest="action", required=True)
    p = actions.add_parser(
        "import", help="draft a verified: false override file from LiteLLM's price file"
    )
    p.add_argument(
        "--from-litellm",
        metavar="SHA",
        help="BerriAI/litellm commit (40 hex characters; never a branch) to download from",
    )
    p.add_argument(
        "--from-file", type=Path, metavar="PATH", help="read a local copy instead (no download)"
    )
    p.add_argument("--out", type=Path, required=True, help="override file to write")
    p.add_argument("--providers", help=f"comma list of {', '.join(PROVIDERS)} (default: all)")
    p.add_argument("--force", action="store_true", help="overwrite --out if it exists")


def fetch(url: str, transport: httpx.BaseTransport | None = None) -> bytes:
    """GET `url` with no redirects and a size cap; only https to raw.githubusercontent.com."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != litellm.RAW_HOST:
        raise CliError(EXIT_USAGE, f"refusing to download from {parts.hostname}")
    try:
        with (
            httpx.Client(transport=transport, timeout=TIMEOUT_SECONDS) as http,
            http.stream("GET", url, follow_redirects=False) as resp,
        ):
            if resp.status_code != httpx.codes.OK:
                raise CliError(EXIT_UNAVAILABLE, f"{url} answered HTTP {resp.status_code}")
            body = bytearray()
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > litellm.MAX_BYTES:
                    raise CliError(EXIT_CONFIG, f"{url} is larger than {litellm.MAX_BYTES} bytes")
    except httpx.HTTPError as exc:
        raise CliError(EXIT_UNAVAILABLE, f"cannot download {url} ({type(exc).__name__})") from None
    return bytes(body)


def _providers(text: str | None) -> frozenset[str]:
    if text is None:
        return frozenset(PROVIDERS)
    wanted = frozenset(p.strip() for p in text.split(",") if p.strip())
    if not wanted or not wanted <= set(PROVIDERS):
        unknown = ", ".join(sorted(wanted - set(PROVIDERS))) or "none given"
        raise CliError(EXIT_USAGE, f"--providers: unknown provider ({unknown})")
    return wanted


def _url(commit: str | None) -> str:
    """The pinned raw URL; for an unnamed local copy, the repository (source_url must be https)."""
    if commit is None:
        return _REPO_URL
    try:
        return litellm.commit_url(commit)
    except litellm.LiteLLMError as exc:
        raise CliError(EXIT_USAGE, f"--from-litellm: {exc}") from None


def _source(args: argparse.Namespace, url: str, fetcher: Fetch) -> litellm.Source:
    """With both flags the file is a copy of that commit: it is read, and nothing is fetched."""
    raw = fetcher(url) if args.from_file is None else _read(args.from_file)
    try:
        data = litellm.parse_litellm(raw)
    except litellm.LiteLLMError as exc:
        raise CliError(EXIT_CONFIG, str(exc)) from None
    return litellm.Source(args.from_litellm, url, hashlib.sha256(raw).hexdigest(), data)


def _read(path: Path) -> bytes:
    try:
        size = path.stat().st_size
        if size > litellm.MAX_BYTES:
            raise CliError(EXIT_CONFIG, f"{path} is larger than {litellm.MAX_BYTES} bytes")
        return path.read_bytes()
    except OSError as exc:
        raise CliError(EXIT_CONFIG, f"cannot read {path} ({type(exc).__name__})") from None


def _bundled() -> dict[str, Any]:
    doc = load_yaml_bytes((resources.files("spendgate.catalog") / "prices.yaml").read_bytes())
    if not isinstance(doc, dict):  # the shipped file; its schema test guards it
        raise CliError(EXIT_CONFIG, "the bundled catalog is not a mapping")
    return doc


def _check(text: str, bundled: dict[str, Any]) -> None:
    """Load the output the way `check-config` loads an override: bundled first, then this."""
    try:
        doc = load_yaml_bytes(text.encode("utf-8"))
        if not isinstance(doc, dict):
            raise CatalogError("not a mapping")
        parse([bundled, doc], "0" * 64, ledger_currency="USD")
    except (yaml.YAMLError, CatalogError) as exc:  # a bug here, not bad input: never write it
        raise CliError(EXIT_CONFIG, f"the drafted file does not load ({exc})") from None


def _write(out: Path, text: str, *, force: bool) -> None:
    try:
        # "x" fails if the file exists: an operator's reviewed override is never clobbered.
        with out.open("w" if force else "x", encoding="utf-8") as fh:
            fh.write(text)
    except FileExistsError:
        raise CliError(EXIT_USAGE, f"{out} exists; pass --force to overwrite it") from None
    except OSError as exc:
        raise CliError(EXIT_CONFIG, f"cannot write {out} ({type(exc).__name__})") from None


def run_import(
    args: argparse.Namespace, *, today: date | None = None, fetcher: Fetch = fetch
) -> int:
    if args.from_litellm is None and args.from_file is None:
        raise CliError(EXIT_USAGE, "give --from-litellm <commit sha> or --from-file <path>")
    providers = _providers(args.providers)
    url = _url(args.from_litellm)
    if args.out.exists() and not args.force:  # checked before any download, and again on open
        raise CliError(EXIT_USAGE, f"{args.out} exists; pass --force to overwrite it")
    day = today or datetime.now(UTC).date()
    source = _source(args, url, fetcher)
    bundled = _bundled()
    carried = {str(m["id"]) for m in bundled["models"]}
    drafts = litellm.draft_overrides(source, carried=carried, providers=providers, today=day)
    for key, reason in drafts.skipped:
        print(f"skipped {key}: {reason}", file=sys.stderr)
    if not drafts.entries:
        raise CliError(EXIT_CONFIG, "nothing to write: no mappable model the catalog lacks")
    body = yaml.safe_dump(
        {"catalog_version": 1, "models": drafts.entries}, sort_keys=False, width=100
    )
    text = litellm.header(source, day) + body
    _check(text, bundled)
    _write(args.out, text, force=args.force)
    print(
        f"wrote {len(drafts.entries)} unverified entries (verified: false) to {args.out} "
        f"({len(drafts.carried)} already in the bundled catalog, {len(drafts.skipped)} skipped); "
        "load it with catalog.paths, then run check-config"
    )
    return EXIT_OK


def main(args: argparse.Namespace) -> int:
    """`spendgate catalog <action>`; `import` is the only action. Takes no config and no keys."""
    return run_import(args)
