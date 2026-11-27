"""Cross-check spendgate's price catalog against LiteLLM's community price file.

A maintainer tool. It uses the standard library, PyYAML and the mapping in `spendgate.core.litellm`
(shared with `spendgate catalog import`), plus `jsonschema`, from the dev group, for `import` and
`override`. It is not product code and nothing in `spendgate` imports it.

    uv run python scripts/litellm_prices.py diff                  # compare; exit 1 on disagreement
    uv run python scripts/litellm_prices.py import gpt-6-luna     # print a draft catalog entry
    uv run python scripts/litellm_prices.py override --out x.yaml # drafts for breadth

LiteLLM's file is a cross-check and a starting point, never a live source: it is read from one
pinned commit, never fetched by the proxy, and nothing here writes into `prices.yaml`. Every entry
it produces is `verified: false` until a person has compared it with the provider's own page.

Provider mapping (LiteLLM `litellm_provider` and key -> our catalog id):

    openai         litellm_provider "openai", bare key                       openai/<key>
    azure_openai   litellm_provider "azure", key azure/<model> (Global)      azure_openai/<model>
                   azure/{us,eu,apac}/<model> are Data Zone prices: used only for the ceiling
                   check below, never as ids. azure/{global,global-standard}/<model> are used
                   when the plain key is absent.
    anthropic      litellm_provider "anthropic", bare key                    anthropic/<key>
    bedrock        litellm_provider "bedrock" or "bedrock_converse", key     bedrock/<key>
                   without "/" (the cross-region profile prefix such as "global." stays part
                   of the name, as in our catalog; region-qualified "bedrock/<region>/..." keys
                   are not mapped)
    google         litellm_provider "gemini" (Gemini API, not Vertex),       google/<name>
                   key with or without the "gemini/" prefix
    elevenlabs     litellm_provider "elevenlabs", key elevenlabs/<name>      elevenlabs/<name>
    azure_speech   LiteLLM has no equivalent: these entries are reported as "ours only"

LiteLLM modes chat, responses, embedding, audio_transcription and audio_speech are mapped; others
(images, video, realtime, rerank, ...) are ignored. Per-token floats become our per-MTok decimal
strings with exact decimal arithmetic: the JSON is parsed with `parse_float=Decimal`, so no binary
float is ever involved.

Like with like. Our Azure entries store Global Standard meters and a `price_multiplier` (the
highest Data Zone uplift). Meters are compared before the multiplier, against LiteLLM's global
key. The multiplier is then checked as a ceiling: our meter times the multiplier must not be below
the dearest Data Zone price LiteLLM lists for the model. Anthropic and Bedrock cache writes are
compared with the 1-hour cache-write price, which is what our catalog stores (never the cheaper
5-minute one). An id with Region-scoped entries (ADR-0028) has one price per Region group, while
LiteLLM has one per id: the group that agrees with LiteLLM is compared, else the cheapest, so a
price of ours below LiteLLM's still fails.

Accepted differences. `scripts/price_diff_accepted.yaml` is a reviewed allow-list: a price that is
HIGHER than LiteLLM's on purpose (an upper bound, a regional meter) can be accepted per id and
meter, with a reason, who accepted it and an expiry of at most 90 days. A price LOWER than
LiteLLM's is never accepted. LiteLLM-only deprecation dates Microsoft or another provider does not
confirm are listed there as "unconfirmed", and a LiteLLM long-context rate the provider does not
bill as "not_offered". Expired items fail again.

Exit codes: 0 nothing to flag; 1 a price we carry differs by more than the threshold, or LiteLLM
lists a deprecation date for a model we carry that we do not (within the horizon); 2 usage or
input error.
"""

import argparse
import hashlib
import json
import sys
import textwrap
import urllib.request
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Context, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from spendgate.core.litellm import (
    MAX_BYTES,
    REPO,
    LiteLLMError,
    LongContext,
    Mapped,
    Source,
    base_entry,
    commit_url,
    convert_meters,
    dec_str,
    draft_entry,
    header,
    index,
    long_contexts,
    map_key,
    parse_litellm,
)

if TYPE_CHECKING:
    from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "src" / "spendgate" / "catalog" / "prices.yaml"
SCHEMA = ROOT / "src" / "spendgate" / "catalog" / "schema.json"

# main of BerriAI/litellm on 2026-11-18. Moving it is a reviewed change: read the diff first.
DEFAULT_COMMIT = "ee133621323bebf27e72695c0b9822d4e8343def"  # pragma: allowlist secret
FILE_PATH = "litellm/model_prices_and_context_window_backup.json"
TIMEOUT_SECONDS = 60

PROVIDERS = (
    "openai",
    "azure_openai",
    "anthropic",
    "bedrock",
    "google",
    "azure_speech",
    "elevenlabs",
)
# Meters that are compared and imported, in the order they are printed.
METERS = (
    "input_per_mtok",
    "cached_input_per_mtok",
    "cache_write_per_mtok",
    "output_per_mtok",
    "reasoning_per_mtok",
    "audio_input_per_mtok",
    "audio_output_per_mtok",
    "audio_input_per_minute",
    "per_1k_characters",
)
_CTX = Context(prec=80)  # far more digits than any price

# Bad arguments or bad input data (exit code 2); the shared mapping raises the same class.
InputError = LiteLLMError


# --- numbers ---------------------------------------------------------------------------------


def pct_diff(ours: Decimal, theirs: Decimal) -> Decimal | None:
    """(theirs - ours) / ours in percent; None when ours is 0 and theirs is not."""
    if ours == theirs:
        return Decimal(0)
    if ours == 0:
        return None
    return _CTX.divide(_CTX.multiply(theirs - ours, Decimal(100)), ours)


def show_pct(pct: Decimal | None) -> str:
    return "n/a (ours is 0)" if pct is None else f"{pct:+.2f}%"


# --- reading LiteLLM -------------------------------------------------------------------------


def raw_url(commit: str) -> str:
    return commit_url(commit, FILE_PATH)


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "spendgate-price-crosscheck"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310
            body: bytes = resp.read(MAX_BYTES + 1)
    except OSError as exc:
        raise InputError(f"cannot download {url} ({type(exc).__name__}: {exc})") from None
    if len(body) > MAX_BYTES:
        raise InputError(f"{url} is larger than {MAX_BYTES} bytes; refusing it")
    return body


def load_source(commit: str, file: Path | None) -> Source:
    url = raw_url(commit)
    if file is not None:
        try:
            raw = file.read_bytes()
        except OSError as exc:
            raise InputError(f"cannot read {file} ({type(exc).__name__})") from None
        if len(raw) > MAX_BYTES:
            raise InputError(f"{file} is larger than {MAX_BYTES} bytes; refusing it")
    else:
        raw = download(url)
    return Source(commit, url, hashlib.sha256(raw).hexdigest(), parse_litellm(raw))


# --- our catalog -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ours:
    id: str
    effective_from: date
    meters: Mapping[str, Decimal]
    multiplier: Decimal
    long_context: LongContext | None
    deprecation: date | None
    raw: Mapping[str, Any]


def _date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def load_ours(path: Path) -> list[Ours]:
    """Our entries, except Region-scoped `google/` ones (`regions`, ADR-0028): those are Vertex AI
    prices, while LiteLLM's `gemini` rows are the Gemini API, so comparing them would be wrong;
    the Vertex importer's tests hold them to Google's source. Region-scoped Bedrock and Azure
    entries are compared, by the group that agrees with LiteLLM, else the cheapest."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        entries = []
        for raw in doc["models"]:
            if raw.get("regions") and str(raw["id"]).startswith("google/"):
                continue  # Vertex AI, not the Gemini API LiteLLM describes
            lc = raw.get("long_context")
            entries.append(
                Ours(
                    raw["id"],
                    _date(raw["effective_from"]),
                    {k: Decimal(v) for k, v in raw["meters"].items() if k in METERS},
                    Decimal(raw.get("price_multiplier", "1")),
                    None
                    if lc is None
                    else LongContext(
                        int(lc["threshold_input_tokens"]),
                        Decimal(lc["input_multiplier"]),
                        Decimal(lc["output_multiplier"]),
                    ),
                    _date(raw["deprecation_date"]) if "deprecation_date" in raw else None,
                    raw,
                )
            )
    except (OSError, yaml.YAMLError, KeyError, TypeError, ArithmeticError, ValueError) as exc:
        raise InputError(f"cannot read the catalog {path} ({type(exc).__name__})") from None
    return entries


# --- diff ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    id: str
    key: str  # LiteLLM key
    kind: str  # "price", "ceiling", "long_context", "deprecation"
    what: str  # meter or field
    ours: str
    theirs: str
    pct: Decimal | None
    failing: bool
    note: str = ""
    accepted: str = ""  # why a reviewed allow-list entry covers this finding


@dataclass(slots=True)
class Report:
    commit: str | None
    sha256: str
    litellm_entries: int
    mappable: int
    threshold: Decimal
    today: date
    horizon_days: int
    exact: list[str] = field(default_factory=list)
    tolerance: list[str] = field(default_factory=list)  # equal within the threshold
    differ: list[str] = field(default_factory=list)
    accepted_ids: list[str] = field(
        default_factory=list
    )  # differ, but all covered by the allow-list
    unused_acceptances: list[str] = field(default_factory=list)
    ours_only: dict[str, str] = field(default_factory=dict)  # id -> reason
    theirs_only: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # our entries not in force today
    findings: list[Finding] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    @property
    def failing(self) -> list[Finding]:
        return [f for f in self.findings if f.failing]


def _in_force(entries: Iterable[Ours], today: date) -> tuple[list[list[Ours]], list[Ours]]:
    """Per id, the entries in force today (several when they are Region-scoped), and the rest."""
    by_id: dict[str, list[Ours]] = defaultdict(list)
    for e in entries:
        by_id[e.id].append(e)
    current, other = [], []
    for group in by_id.values():
        group.sort(key=lambda e: e.effective_from, reverse=True)
        live = [e for e in group if e.effective_from <= today]
        newest = [e for e in live if e.effective_from == live[0].effective_from] if live else []
        if newest:
            current.append(newest)
        other += [e for e in group if all(e is not n for n in newest)]
    return current, other


def _closest(
    alternatives: Sequence[Ours], theirs: Mapped, conv: Mapping[str, Decimal], thr: Decimal
) -> Ours:
    """The entry to compare when an id has Region-scoped prices (ADR-0028).

    LiteLLM has one price per id, for some Region. If one of our Region prices agrees with it,
    that one is compared; otherwise the cheapest, so a price of ours below LiteLLM's still fails.
    """
    for ours in alternatives:
        if not any(f.failing for f in _compare_price(ours, theirs, conv, thr)[0]):
            return ours
    big = Decimal("Infinity")
    return min(
        alternatives,
        key=lambda e: (e.meters.get("input_per_mtok", big), e.meters.get("output_per_mtok", big)),
    )


def _compare_price(
    ours: Ours, theirs: Mapped, conv: Mapping[str, Decimal], thr: Decimal
) -> tuple[list[Finding], bool]:
    """Findings for one entry, and whether any carried meter differed only within tolerance."""
    findings: list[Finding] = []
    inexact = False
    for meter in METERS:
        if meter not in ours.meters:
            continue
        mine = ours.meters[meter]
        if meter not in conv:
            findings.append(
                Finding(
                    ours.id,
                    theirs.key,
                    "price",
                    meter,
                    dec_str(mine),
                    "-",
                    None,
                    False,
                    "LiteLLM has no such meter",
                )
            )
            continue
        pct = pct_diff(mine, conv[meter])
        if pct == 0 and mine == conv[meter]:
            continue
        failing = pct is None or abs(pct) > thr
        inexact = inexact or not failing
        if failing or pct != 0:
            findings.append(
                Finding(
                    ours.id,
                    theirs.key,
                    "price",
                    meter,
                    dec_str(mine),
                    dec_str(conv[meter]),
                    pct,
                    failing,
                )
            )
    return findings, inexact


def _compare_long_context(ours: Ours, theirs: Mapped, thr: Decimal) -> list[Finding]:
    mine = ours.long_context
    listed = long_contexts(theirs.spec)
    if mine is None:
        if not listed:
            return []
        lc = listed[0]
        return [
            Finding(
                ours.id,
                theirs.key,
                "long_context",
                "long_context",
                "none",
                f">{lc.threshold} tokens: input x{dec_str(lc.input_multiplier)}, "
                f"output x{dec_str(lc.output_multiplier)}",
                None,
                True,
                "LiteLLM lists a long-context price we do not carry",
            )
        ]
    match = [lc for lc in listed if lc.threshold == mine.threshold]
    if not match:
        theirs_text = ", ".join(str(lc.threshold) for lc in listed) or "none"
        return [
            Finding(
                ours.id,
                theirs.key,
                "long_context",
                "threshold_input_tokens",
                str(mine.threshold),
                theirs_text,
                None,
                True,
                "different long-context threshold",
            )
        ]
    out = []
    for name, a, b in (
        ("input_multiplier", mine.input_multiplier, match[0].input_multiplier),
        ("output_multiplier", mine.output_multiplier, match[0].output_multiplier),
    ):
        pct = pct_diff(a, b)
        if pct is None or abs(pct) > thr:
            out.append(
                Finding(
                    ours.id, theirs.key, "long_context", name, dec_str(a), dec_str(b), pct, True
                )
            )
    return out


def _compare_ceiling(
    ours: Ours, theirs_key: str, zones: Sequence[Mapped], thr: Decimal
) -> list[Finding]:
    """Our meter x multiplier must not be below the dearest Data Zone price LiteLLM lists."""
    if ours.multiplier == 1 or not zones:
        return []
    out = []
    for meter in METERS:
        if meter not in ours.meters:
            continue
        prices = [c[meter] for z in zones if meter in (c := convert_meters("azure_openai", z.spec))]
        if not prices:
            continue
        top, mine = max(prices), _CTX.multiply(ours.meters[meter], ours.multiplier)
        pct = pct_diff(mine, top)
        if pct is None or pct > thr:
            out.append(
                Finding(
                    ours.id,
                    theirs_key,
                    "ceiling",
                    meter,
                    dec_str(mine),
                    dec_str(top),
                    pct,
                    True,
                    f"x{dec_str(ours.multiplier)} is below the dearest Data Zone price",
                )
            )
    return out


def _compare_deprecation(ours: Ours, theirs: Mapped, today: date, horizon: int) -> Finding | None:
    raw = theirs.spec.get("deprecation_date")
    if raw is None:
        return None
    try:
        their_date = date.fromisoformat(str(raw))
    except ValueError:
        return None
    if ours.deprecation == their_date:
        return None
    soon = their_date <= today + timedelta(days=horizon)
    note = "LiteLLM lists a deprecation date we do not have"
    if ours.deprecation is not None:
        note = "deprecation dates differ"
    if not soon:
        note += f" (beyond the {horizon}-day horizon: informational)"
    return Finding(
        ours.id,
        theirs.key,
        "deprecation",
        "deprecation_date",
        ours.deprecation.isoformat() if ours.deprecation else "none",
        their_date.isoformat(),
        None,
        soon,
        note,
    )


# --- reviewed allow-list ---------------------------------------------------------------------

ACCEPTED = ROOT / "scripts" / "price_diff_accepted.yaml"
MAX_ACCEPTANCE_DAYS = 90
# unconfirmed: only LiteLLM has a date. ours_earlier: our date comes from the provider's own
# schedule and is earlier than LiteLLM's, so the catalog refuses the model sooner (fail closed).
DEPRECATION_STATUSES = frozenset({"unconfirmed", "ours_earlier"})
# not_offered: LiteLLM lists a long-context rate the provider does not bill: its own price list
# has no long-context price, and its model card caps the context at the threshold or below.
LONG_CONTEXT_STATUSES = frozenset({"not_offered"})
OURS_EARLIER_LABEL = "[ours earlier] "


@dataclass(frozen=True, slots=True)
class Acceptance:
    id: str
    what: frozenset[str]  # meters (prices), {"deprecation_date"} or {"long_context"}
    litellm_date: date | None  # deprecations only: the exact date being accepted
    reason: str
    accepted_by: str
    accepted_on: date
    expires: date
    status: str = ""  # deprecations: "unconfirmed" or "ours_earlier"; long contexts: "not_offered"
    litellm_threshold: int | None = None  # long contexts only: LiteLLM's exact threshold

    def label(self) -> str:
        who = f"accepted by {self.accepted_by} on {self.accepted_on}, expires {self.expires}"
        prefix = OURS_EARLIER_LABEL if self.status == "ours_earlier" else ""
        return f"{prefix}{self.reason} ({who})"


def _status(raw: Mapping[str, Any], entry_id: str, allowed: frozenset[str]) -> str:
    status = str(raw["status"])
    if status not in allowed:
        raise InputError(
            f"accepted list: {entry_id}: status must be one of " + ", ".join(sorted(allowed))
        )
    return status


def _acceptance(raw: object, kind: str) -> Acceptance:
    """One allow-list item; `kind` is "difference", "deprecation" or "long_context"."""
    if not isinstance(raw, dict):
        raise InputError("accepted list: every item must be a mapping")
    lit, threshold, status = None, None, ""
    try:
        entry_id = str(raw["id"])
        reason, by = str(raw["reason"]).strip(), str(raw["accepted_by"]).strip()
        on, expires = _date(raw["accepted_on"]), _date(raw["expires"])
        if kind == "difference":
            if raw["relation"] != "ours_higher":
                raise InputError(
                    f"accepted list: {entry_id}: relation must be ours_higher "
                    "(a price below LiteLLM's is never accepted)"
                )
            meters = raw["meters"]
            if not isinstance(meters, list) or not meters or not set(meters) <= set(METERS):
                raise InputError(
                    f"accepted list: {entry_id}: meters must be a list of known meters"
                )
            what = frozenset(meters)
        elif kind == "deprecation":
            what, lit = frozenset({"deprecation_date"}), _date(raw["litellm_date"])
            status = _status(raw, entry_id, DEPRECATION_STATUSES)
        else:
            what, threshold = frozenset({"long_context"}), int(raw["litellm_threshold"])
            status = _status(raw, entry_id, LONG_CONTEXT_STATUSES)
    except (KeyError, TypeError, ValueError) as exc:
        raise InputError(f"accepted list: bad item ({type(exc).__name__}: {exc})") from None
    if not reason or not by:
        raise InputError(f"accepted list: {entry_id}: reason and accepted_by are required")
    if not on < expires <= on + timedelta(days=MAX_ACCEPTANCE_DAYS):
        raise InputError(
            f"accepted list: {entry_id}: expires must be after accepted_on and at most "
            f"{MAX_ACCEPTANCE_DAYS} days later"
        )
    return Acceptance(entry_id, what, lit, reason, by, on, expires, status, threshold)


def load_accepted(path: Path) -> list[Acceptance]:
    """The reviewed allow-list; a missing file means nothing is accepted."""
    if not path.exists():
        return []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise InputError(f"cannot read {path} ({type(exc).__name__})") from None
    if not isinstance(doc, dict):
        raise InputError(f"{path}: must be a mapping")
    sections = (
        ("differences", "difference"),
        ("deprecations", "deprecation"),
        ("long_contexts", "long_context"),
    )
    return [_acceptance(r, kind) for key, kind in sections for r in doc.get(key) or []]


def _earlier(ours: str, theirs: str) -> bool:
    try:
        return date.fromisoformat(ours) < date.fromisoformat(theirs)
    except ValueError:  # "none": we have no date, so nothing of ours refuses sooner
        return False


def _cover(f: Finding, accepted: Sequence[Acceptance], today: date, used: set[int]) -> Finding:
    """`f` marked accepted when a live allow-list item covers it; never for a lower price."""
    for i, a in enumerate(accepted):
        if a.id != f.id or f.what not in a.what:
            continue
        if f.kind == "deprecation":
            if a.litellm_date is None or a.litellm_date.isoformat() != f.theirs:
                continue
            if a.status == "ours_earlier" and not _earlier(f.ours, f.theirs):
                continue  # only a date of ours that refuses sooner can be accepted
        elif f.kind == "long_context":
            # Only "we carry none, LiteLLM lists one at exactly this threshold"; a rule of
            # ours that differs from LiteLLM's is never accepted.
            if f.ours != "none" or not f.theirs.startswith(f">{a.litellm_threshold} tokens:"):
                continue
        elif f.pct is None or f.pct >= 0 or f.theirs == "-":
            continue  # ours is lower (or not comparable): only ours_higher can be accepted
        used.add(i)
        if a.expires < today:
            return replace(
                f,
                note=f"{f.note}; acceptance expired on {a.expires}: review again".lstrip("; "),
                failing=True,
            )
        return replace(f, failing=False, accepted=a.label())
    return f


def diff_catalogs(  # noqa: PLR0913
    source: Source,
    entries: Sequence[Ours],
    *,
    threshold: Decimal,
    today: date,
    horizon_days: int,
    accepted: Sequence[Acceptance] = (),
) -> Report:
    found = index(source.data)
    used: set[int] = set()
    report = Report(
        source.commit,
        source.sha256,
        len(source.data),
        sum(len(v) for variants in found.values() for v in variants.values()),
        threshold,
        today,
        horizon_days,
    )
    current, other = _in_force(entries, today)
    report.skipped = sorted(f"{e.id}@{e.effective_from}" for e in other)
    carried = {e.id for e in entries}
    for alternatives in sorted(current, key=lambda g: g[0].id):
        ours = alternatives[0]
        variants = found.get(ours.id, {})
        theirs = base_entry(variants)
        if theirs is None:
            has_zone = bool(variants.get("zone"))
            report.ours_only[ours.id] = (
                "LiteLLM has no equivalent provider"
                if ours.id.startswith("azure_speech/")
                else "only Data Zone keys in LiteLLM"
                if has_zone
                else "no LiteLLM entry"
            )
            continue
        conv = convert_meters(ours.id.partition("/")[0], theirs.spec)
        if len(alternatives) > 1:
            ours = _closest(alternatives, theirs, conv, threshold)
        findings, inexact = _compare_price(ours, theirs, conv, threshold)
        findings += _compare_long_context(ours, theirs, threshold)
        findings += _compare_ceiling(ours, theirs.key, variants.get("zone", []), threshold)
        if (dep := _compare_deprecation(ours, theirs, today, horizon_days)) is not None:
            findings.append(dep)
        findings = [_cover(f, accepted, today, used) for f in findings]
        report.findings += findings
        money = [f for f in findings if f.kind != "deprecation" and (f.failing or f.pct != 0)]
        if any(f.failing for f in money):
            report.differ.append(ours.id)
        elif any(f.accepted for f in money):
            report.accepted_ids.append(ours.id)
        elif inexact:
            report.tolerance.append(ours.id)
        else:
            report.exact.append(ours.id)
    report.theirs_only = sorted(i for i in found if i not in carried)
    report.unused_acceptances = [
        f"{a.id} [{', '.join(sorted(a.what))}]" for i, a in enumerate(accepted) if i not in used
    ]
    return report


def render(report: Report) -> str:  # noqa: PLR0912
    thr = f"{report.threshold:f}".rstrip("0").rstrip(".")
    compared = (
        len(report.exact) + len(report.tolerance) + len(report.differ) + len(report.accepted_ids)
    )
    lines = [
        f"LiteLLM {REPO} @ {report.commit}",
        f"file {FILE_PATH}: sha256 {report.sha256}",
        f"{report.litellm_entries} entries, {report.mappable} mappable to our providers",
        f"threshold {thr}%, as of {report.today}, deprecation horizon {report.horizon_days} days",
        "",
        "SUMMARY",
        f"  our entries in force: {compared + len(report.ours_only)}",
        f"  in both, equal:       {len(report.exact) + len(report.tolerance)}"
        f" (exact {len(report.exact)}, within tolerance {len(report.tolerance)})",
        f"  in both, different:   {len(report.differ)}"
        f" (plus {len(report.accepted_ids)} accepted, ours higher)",
        f"  ours only:            {len(report.ours_only)}",
        f"  theirs only:          {len(report.theirs_only)} (for our providers)",
        f"  not compared:         {len(report.skipped)} older or future entries of the same ids",
        f"  failing findings:     {len(report.failing)}",
    ]
    money = [f for f in report.findings if f.kind != "deprecation"]
    lines += ["", "PRICES THAT DIFFER (per meter; ours vs LiteLLM)"]
    for entry_id in report.differ:
        lines.append(f"  {entry_id}")
        for f in (x for x in money if x.id == entry_id):
            if f.theirs == "-":
                lines.append(f"    [{f.kind}] {f.what}: ours {f.ours}, not listed by LiteLLM")
                continue
            where = "ours HIGHER" if f.pct is not None and f.pct < 0 else "ours LOWER"
            if f.pct is None:
                where = "check"
            flag = "" if f.failing else " (within threshold)"
            lines.append(
                f"    [{f.kind}] {f.what}: ours {f.ours}, LiteLLM {f.theirs}, "
                f"{show_pct(f.pct)}, {where}{flag}" + (f" - {f.note}" if f.note else "")
            )
    if not report.differ:
        lines.append("  none")
    if report.accepted_ids:
        lines += ["", "ACCEPTED DIFFERENCES (reviewed allow-list; ours HIGHER)"]
        for entry_id in report.accepted_ids:
            lines.append(f"  {entry_id}")
            for f in (x for x in money if x.id == entry_id and x.accepted):
                lines.append(
                    f"    {f.what}: ours {f.ours}, LiteLLM {f.theirs}, {show_pct(f.pct)}"
                    f" - {f.accepted}"
                )
    lines += ["", "DEPRECATION DATES IN LITELLM"]
    deps = [f for f in report.findings if f.kind == "deprecation"]
    for f in deps:
        status = f.note
        if f.accepted:
            if f.accepted.startswith(OURS_EARLIER_LABEL):
                status = f"OURS EARLIER, accepted: {f.accepted.removeprefix(OURS_EARLIER_LABEL)}"
            else:
                status = f"UNCONFIRMED, accepted: {f.accepted}"
        lines.append(f"  {f.id}: ours {f.ours}, LiteLLM {f.theirs} - {status}")
    if not deps:
        lines.append("  none")
    notes = [f for f in money if not f.failing and not f.accepted and f.id not in report.differ]
    if notes:
        lines += ["", "OTHER NOTES (not failing)"]
        lines += [f"  {f.id} [{f.what}]: ours {f.ours}, LiteLLM {f.theirs} {f.note}" for f in notes]
    if report.unused_acceptances:
        lines += ["", "ALLOW-LIST ITEMS THAT MATCH NOTHING TODAY (remove them)"]
        lines += [f"  {u}" for u in report.unused_acceptances]
    lines += ["", "OURS ONLY"]
    lines += [f"  {i}: {why}" for i, why in report.ours_only.items()] or ["  none"]
    lines += ["", "THEIRS ONLY (mappable, not in our catalog)"]
    by_provider: dict[str, list[str]] = defaultdict(list)
    for i in report.theirs_only:
        by_provider[i.partition("/")[0]].append(i.partition("/")[2])
    for slug in PROVIDERS:
        if by_provider[slug]:
            text = f"  {slug} ({len(by_provider[slug])}): " + ", ".join(by_provider[slug])
            lines += textwrap.wrap(text, 110, subsequent_indent="    ", break_on_hyphens=False)
    if report.skipped:
        lines += ["", "NOT COMPARED (not the entry in force today)"]
        lines += [f"  {s}" for s in report.skipped]
    return "\n".join(lines) + "\n"


# --- import and override ---------------------------------------------------------------------


def _validator() -> "Draft202012Validator":
    try:
        from jsonschema import Draft202012Validator  # noqa: PLC0415
    except ImportError:
        raise InputError("jsonschema is needed to validate output: run `uv sync`") from None
    return Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))


def schema_errors(validator: "Draft202012Validator", entry: Mapping[str, Any]) -> list[str]:
    doc = {"catalog_version": 1, "models": [entry]}
    return sorted(
        f"{'/'.join(str(p) for p in e.absolute_path) or '.'}: {e.message}"
        for e in validator.iter_errors(doc)
    )


def dump_catalog(entries: Sequence[Mapping[str, Any]], top: str) -> str:
    body = yaml.safe_dump(
        {"catalog_version": 1, "models": list(entries)}, sort_keys=False, width=100
    )
    return top + body


def run_import(source: Source, names: Sequence[str], today: date) -> tuple[str, list[str]]:
    validator = _validator()
    entries, problems = [], []
    for name in names:
        spec = source.data.get(name)
        m = None if spec is None else map_key(name, spec)
        if m is None or m.variant == "zone":
            problems.append(f"{name}: not a mappable LiteLLM entry for our providers")
            continue
        entry = draft_entry(m, source, today)
        if errors := schema_errors(validator, entry):
            problems.append(f"{name}: draft does not validate ({'; '.join(errors)})")
            continue
        entries.append(entry)
    return dump_catalog(entries, header(source, today)), problems


def run_override(source: Source, ours: Iterable[Ours], today: date) -> tuple[str, list[str]]:
    validator = _validator()
    have = {e.id for e in ours}
    entries, skipped = [], []
    for entry_id, variants in sorted(index(source.data).items()):
        if entry_id in have or (m := base_entry(variants)) is None:
            continue
        entry = draft_entry(m, source, today)
        if schema_errors(validator, entry):
            skipped.append(m.key)
            continue
        entries.append(entry)
    return dump_catalog(entries, header(source, today)), skipped


# --- command line ----------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    for name, text in (
        ("diff", "compare our catalog with LiteLLM's"),
        ("import", "draft catalog entries from named LiteLLM entries"),
        ("override", "draft entries for every mappable LiteLLM model we do not carry"),
    ):
        s = sub.add_parser(name, help=text)
        s.add_argument("--commit", default=DEFAULT_COMMIT, help="BerriAI/litellm commit SHA")
        s.add_argument("--file", type=Path, help="read this local copy instead of downloading")
        s.add_argument("--catalog", type=Path, default=CATALOG, help="our catalog (prices.yaml)")
        s.add_argument("--today", type=date.fromisoformat, default=None, help="override today")
        s.add_argument("--out", type=Path, help="write here instead of stdout")
        if name == "diff":
            s.add_argument("--threshold", type=Decimal, default=Decimal(1), help="percent (1)")
            s.add_argument("--horizon-days", type=int, default=180, help="deprecations (180)")
            s.add_argument(
                "--accepted", type=Path, default=ACCEPTED, help="reviewed allow-list of differences"
            )
        if name == "import":
            s.add_argument("names", nargs="+", help="LiteLLM keys, e.g. gpt-6-luna")
    return p


def _emit(text: str, out: Path | None) -> None:
    if out is None:
        sys.stdout.write(text)
    else:
        out.write_text(text, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    today: date = args.today or datetime.now(UTC).date()
    try:
        source = load_source(args.commit, args.file)
        ours = load_ours(args.catalog)
        if args.command == "diff":
            report = diff_catalogs(
                source,
                ours,
                threshold=args.threshold,
                today=today,
                horizon_days=args.horizon_days,
                accepted=load_accepted(args.accepted),
            )
            text = render(report)
            sys.stdout.write(text)
            if args.out is not None:
                args.out.write_text(text, encoding="utf-8")
            return 1 if report.failing else 0
        if args.command == "import":
            text, problems = run_import(source, args.names, today)
        else:
            text, problems = run_override(source, ours, today)
            if problems:
                print(f"skipped {len(problems)} entries that do not validate", file=sys.stderr)
                problems = []
    except (InputError, ArithmeticError) as exc:  # ArithmeticError: an exponent out of range
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _emit(text, args.out)
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
