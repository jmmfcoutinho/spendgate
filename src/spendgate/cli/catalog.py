"""`spendgate catalog import` and `catalog draft`: write a `verified: false` override file.

Operator actions at a desk, never part of serving: the proxy and library mode read only local
catalog files (ADR-0014). `import` reads one pinned commit of BerriAI/litellm over https on
raw.githubusercontent.com, or a local copy, and drafts the models the bundled catalog does not
carry. `draft` prices one model like another of the same provider, times a multiplier of at
least 1 (ADR-0028). An existing file is never overwritten without `--force`.

Exit codes: 0 written; 2 bad command line (for `draft`, also a `--like` with no price in force),
or the output exists; 3 the input is not LiteLLM's JSON, nothing could be drafted, or the
configuration cannot be read; 5 the download failed.
"""

import argparse
import hashlib
import os
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

from spendgate.cli.client import EXIT_CONFIG, EXIT_OK, EXIT_UNAVAILABLE, EXIT_USAGE, CliError
from spendgate.config import ConfigError, read_catalog
from spendgate.config.yamlio import load_yaml_bytes
from spendgate.core import litellm, standin
from spendgate.core.catalog import (
    PROVIDERS,
    Catalog,
    CatalogError,
    ModelNotPriced,
    PriceEntry,
    parse,
)
from spendgate.core.money import FxSnapshot

TIMEOUT_SECONDS = 60.0
_REPO_URL = f"https://github.com/{litellm.REPO}"  # source_url of a copy whose commit is unnamed
_NO_SHA = "0" * 64  # a check-only load: no call row records this catalog
_DRAFT_HEADER = """\
# Stand-in drafted by `spendgate catalog draft` on {today} (UTC). verified: false.
# {as_id} is priced like {price_id} x {multiplier}: a borrowed price, not the provider's.
# Replace it with the provider's own price and set verified: true once checked.
"""

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
    _add_draft(actions)


def _add_draft(actions: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    d = actions.add_parser(
        "draft",
        help="price a model the catalog lacks like another one (a verified: false stand-in)",
        description="Write an override file pricing --as like --like (same provider), every "
        "meter times --multiplier. The entry is verified: false and its note names the copied "
        "entry; replace it with the provider's price.",
    )
    d.add_argument("--like", required=True, metavar="ID", help="priced catalog id to copy")
    d.add_argument("--as", dest="as_id", required=True, metavar="ID", help="catalog id to price")
    d.add_argument(
        "--multiplier", default="1", help="decimal of at least 1 applied to every meter (1)"
    )
    d.add_argument("--config", help="also read the override files in this config's catalog.paths")
    d.add_argument("--out", type=Path, required=True, help="override file to write")
    d.add_argument("--force", action="store_true", help="overwrite --out if it exists")


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
    """At most MAX_BYTES; a FIFO, a device or a growing file cannot make it read more."""
    try:
        with path.open("rb") as fh:
            raw = fh.read(litellm.MAX_BYTES + 1)
    except OSError as exc:
        raise CliError(EXIT_CONFIG, f"cannot read {path} ({type(exc).__name__})") from None
    if len(raw) > litellm.MAX_BYTES:
        raise CliError(EXIT_CONFIG, f"{path} is larger than {litellm.MAX_BYTES} bytes")
    return raw


def _bundled() -> dict[str, Any]:
    doc = load_yaml_bytes((resources.files("spendgate.catalog") / "prices.yaml").read_bytes())
    if not isinstance(doc, dict):  # the shipped file; its schema test guards it
        raise CliError(EXIT_CONFIG, "the bundled catalog is not a mapping")
    return doc


def _check(
    text: str,
    before: list[Any],
    ledger_currency: str = "USD",
    fx: tuple[FxSnapshot, ...] = (),
) -> None:
    """Load the output the way `check-config` loads an override: after `before` (bundled first)."""
    try:
        doc = load_yaml_bytes(text.encode("utf-8"))
        if not isinstance(doc, dict):
            raise CatalogError("not a mapping")
        parse([*before, doc], _NO_SHA, ledger_currency=ledger_currency, fx=fx)
    except (yaml.YAMLError, CatalogError) as exc:  # a bug here, not bad input: never write it
        raise CliError(EXIT_CONFIG, f"the drafted file does not load ({exc})") from None


def _write(out: Path, text: str, *, force: bool) -> None:
    try:
        if force:
            _replace(out, text)
            return
        # "x" fails if the file exists: an operator's reviewed override is never clobbered.
        with out.open("x", encoding="utf-8") as fh:
            fh.write(text)
    except FileExistsError:
        raise CliError(EXIT_USAGE, f"{out} exists; pass --force to overwrite it") from None
    except OSError as exc:
        raise CliError(EXIT_CONFIG, f"cannot write {out} ({type(exc).__name__})") from None


def _replace(out: Path, text: str) -> None:
    """Write beside `out`, fsync, then rename over it: a crash leaves the old file or the new."""
    fd, name = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)  # gone after a successful replace


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
    try:
        drafts = litellm.draft_overrides(source, carried=carried, providers=providers, today=day)
    except (litellm.LiteLLMError, ArithmeticError) as exc:
        raise CliError(EXIT_CONFIG, f"the LiteLLM file cannot be mapped ({exc})") from None
    for key, reason in drafts.skipped:
        # repr: a key is third-party text, and a raw control sequence would drive the terminal.
        print(f"skipped {key!r}: {reason}", file=sys.stderr)
    if not drafts.entries:
        raise CliError(EXIT_CONFIG, "nothing to write: no mappable model the catalog lacks")
    body = yaml.safe_dump(
        {"catalog_version": 1, "models": drafts.entries}, sort_keys=False, width=100
    )
    text = litellm.header(source, day) + body
    _check(text, [bundled])
    _write(args.out, text, force=args.force)
    print(
        f"wrote {len(drafts.entries)} unverified entries (verified: false) to {args.out} "
        f"({len(drafts.carried)} already in the bundled catalog, {len(drafts.skipped)} skipped); "
        "load it with catalog.paths, then run check-config"
    )
    return EXIT_OK


def _loaded(config: str | None) -> tuple[Catalog, list[Any]]:
    """Bundled only, or with `--config` also its override files (no secret is read)."""
    if config is None:
        bundled = _bundled()
        return parse([bundled], _NO_SHA, ledger_currency="USD"), [bundled]
    try:
        catalog, docs = read_catalog(config)
    except ConfigError as exc:
        raise CliError(EXIT_CONFIG, str(exc)) from None
    return catalog, list(docs)


def _draft_args(args: argparse.Namespace) -> None:
    """Command-line problems, before any file is read."""
    try:
        standin.check_ids(args.like, args.as_id)
    except standin.StandInError as exc:
        raise CliError(EXIT_USAGE, str(exc)) from None
    try:
        standin.parse_multiplier(args.multiplier)
    except standin.StandInError as exc:
        raise CliError(EXIT_USAGE, f"--multiplier: {exc}") from None
    if args.out.exists() and not args.force:  # checked first, and again on open
        raise CliError(EXIT_USAGE, f"{args.out} exists; pass --force to overwrite it")


def _sources(catalog: Catalog, like: str, day: date, now_ms: int) -> list[PriceEntry]:
    """The entry of `like` in force today, then its scheduled ones: the stand-in follows them."""
    provider, _, model = like.partition("/")
    try:
        current = catalog.lookup(provider, model, now_ms)
    except ModelNotPriced as exc:
        raise CliError(
            EXIT_USAGE, f"--like: {like} has no price in force to copy ({exc.reason})"
        ) from None
    later = (e for e in catalog.entries() if e.id == like and e.effective_from > day)
    return [current, *sorted(later, key=lambda e: e.effective_from)]


def run_draft(args: argparse.Namespace, *, today: date | None = None) -> int:
    _draft_args(args)
    day = today or datetime.now(UTC).date()
    now_ms = int(datetime.combine(day, time(), tzinfo=UTC).timestamp()) * 1000
    catalog, docs = _loaded(args.config)
    sources = _sources(catalog, args.like, day, now_ms)
    try:
        entries = [standin.draft(s, args.as_id, args.multiplier, day) for s in sources]
    except standin.StandInError as exc:
        raise CliError(EXIT_CONFIG, str(exc)) from None
    _warn_if_priced(catalog, args.as_id, now_ms, day)
    head = _DRAFT_HEADER.format(
        today=day, as_id=args.as_id, price_id=sources[0].price_id, multiplier=args.multiplier
    )
    if len(sources) > 1:
        head += f"# It follows the scheduled prices of {args.like} too: one entry per date.\n"
    body = yaml.safe_dump({"catalog_version": 1, "models": entries}, sort_keys=False, width=100)
    _check(head + body, docs, catalog.ledger_currency, catalog.fx)
    _write(args.out, head + body, force=args.force)
    print(
        f"wrote a stand-in for {args.as_id} (verified: false, {len(entries)} "
        f"{'entry' if len(entries) == 1 else 'entries'}) to {args.out}, priced like "
        f"{sources[0].price_id} x {args.multiplier}; load it with catalog.paths, then run "
        "check-config"
    )
    return EXIT_OK


def _warn_if_priced(catalog: Catalog, as_id: str, now_ms: int, day: date) -> None:
    """Allowed (an operator may mark a price up), but a reviewed price gives way: say so."""
    provider, _, model = as_id.partition("/")
    try:
        current = catalog.lookup(provider, model, now_ms)
    except ModelNotPriced:
        return
    print(
        f"warning: {as_id} is already priced ({current.price_id}); from {day} the stand-in "
        "supersedes it, and check-config warns if that price was verified",
        file=sys.stderr,
    )


def main(args: argparse.Namespace) -> int:
    """`spendgate catalog import|draft`. Neither takes an admin key; only `draft` reads a config."""
    return run_draft(args) if args.action == "draft" else run_import(args)
