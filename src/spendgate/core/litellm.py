"""Map LiteLLM's community price file onto catalog entries: bytes and dicts in, dicts out.

Used by `spendgate catalog import` and `scripts/litellm_prices.py`; never at serve time. The
caller reads or downloads the file. Every drafted entry is `verified: false`, and an entry that
would not load (a chat model without an output ceiling, above all) is skipped with a reason,
never repaired with a guessed value.

Provider mapping (LiteLLM `litellm_provider` and key -> our catalog id):

    openai         "openai", bare key                                  openai/<key>
    azure_openai   "azure", key azure/<model> (Global); azure/{global,  azure_openai/<model>
                   global-standard}/<model> when the plain key is absent; azure/{us,eu,apac}/...
                   are Data Zone prices, never ids
    anthropic      "anthropic", bare key                               anthropic/<key>
    bedrock        "bedrock" or "bedrock_converse", key without "/"    bedrock/<key>
    google         "gemini" (Gemini API, not Vertex), "gemini/" dropped google/<name>
    elevenlabs     "elevenlabs", key elevenlabs/<name>                 elevenlabs/<name>

Modes chat, responses, embedding, audio_transcription and audio_speech are mapped; others are
ignored. Per-token numbers become per-MTok decimal strings with exact decimal arithmetic: the
JSON is parsed with `parse_float=Decimal`, so no binary float is ever involved.
"""

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Set
from dataclasses import dataclass, field
from datetime import date
from decimal import Context, Decimal
from typing import Any

from pydantic import ValidationError

from spendgate.core.catalog import PriceEntry

REPO = "BerriAI/litellm"
RAW_HOST = "raw.githubusercontent.com"
FILE_PATH = "model_prices_and_context_window.json"
MAX_BYTES = 32 * 1024 * 1024
ZONES = frozenset({"us", "eu", "apac"})
GLOBAL_ALIASES = frozenset({"global", "global-standard"})
LITELLM_MODES = {
    "chat": "chat",
    "responses": "responses",
    "embedding": "embedding",
    "audio_transcription": "transcription",
    "audio_speech": "speech",
}
_MILLION = Decimal(1_000_000)
_CTX = Context(prec=80)  # far more digits than any price: multiplication here is exact
_SHA = re.compile(r"[0-9a-f]{40}")
_ABOVE = re.compile(r"^input_cost_per_token_above_(\d+)k_tokens$")
_CHAT_MODES = frozenset({"chat", "responses", "messages"})
_NOTE_MAX = 300  # PriceEntry.note
_MAX_EXPONENT = 9  # largest accepted price per catalog unit: under 10^10

type Spec = Mapping[str, Any]


class LiteLLMError(ValueError):
    """Bad input: not a commit SHA, not LiteLLM's JSON, not a number."""


# --- numbers ---------------------------------------------------------------------------------


def to_decimal(value: object) -> Decimal:
    """A JSON number as an exact Decimal (parsed with `parse_float=Decimal`, never a float).

    NaN and Infinity reach here as floats (json's `parse_constant`), strings and booleans as
    themselves: all are refused. The message names the type only, never the value.
    """
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise LiteLLMError(f"not a JSON number ({type(value).__name__})")
    number = Decimal(value)
    if not number.is_finite():
        raise LiteLLMError("not a finite number")
    return number


def whole(value: object) -> int:
    """A JSON number that must be a whole number (a token limit): never rounded."""
    number = to_decimal(value)
    if number != number.to_integral_value():
        raise LiteLLMError("not a whole number")
    return int(number)


def scale(value: object, factor: Decimal) -> Decimal:
    """`value` x `factor`, refused above 10^10 per unit: no real meter is near that, and the
    decimal string of a huge exponent would be megabytes long."""
    scaled = _CTX.multiply(to_decimal(value), factor)
    if scaled.adjusted() > _MAX_EXPONENT:
        raise LiteLLMError("a price is out of range")
    return scaled


def dec_str(value: Decimal) -> str:
    """Plain decimal string without exponent or trailing zeros ("0.08", "10", "0")."""
    return format(value.normalize(), "f")


# --- the source file -------------------------------------------------------------------------


def commit_url(commit: str, file_path: str = FILE_PATH) -> str:
    """The raw file at one commit. A branch or tag could move under the reader: only a SHA."""
    if not _SHA.fullmatch(commit):
        raise LiteLLMError("the LiteLLM commit must be a full 40-character lower-case commit SHA")
    return f"https://{RAW_HOST}/{REPO}/{commit}/{file_path}"


@dataclass(frozen=True, slots=True)
class Source:
    commit: str | None  # None: a local copy whose commit the operator did not name
    url: str  # https, recorded as each draft's source_url
    sha256: str  # of the bytes read
    data: Mapping[str, Spec]


def parse_litellm(raw: bytes) -> Mapping[str, Spec]:
    if len(raw) > MAX_BYTES:
        raise LiteLLMError(f"the LiteLLM file is larger than {MAX_BYTES} bytes; refusing it")
    try:
        doc = json.loads(raw, parse_float=Decimal)
    except (ValueError, RecursionError) as exc:
        raise LiteLLMError(f"the LiteLLM file is not valid JSON ({type(exc).__name__})") from None
    if not isinstance(doc, dict):
        raise LiteLLMError("the LiteLLM file is not a JSON object")
    return {k: v for k, v in doc.items() if isinstance(v, dict) and k != "sample_spec"}


# --- mapping and conversion ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Mapped:
    key: str  # LiteLLM key
    id: str  # our catalog id
    variant: str  # "base", "alt" (azure global alias) or "zone" (azure Data Zone)
    mode: str  # our mode family: chat, responses, embedding, transcription, speech
    spec: Spec


_SIMPLE = {  # litellm_provider -> (our slug, key prefix LiteLLM may add)
    "openai": ("openai", ""),
    "anthropic": ("anthropic", ""),
    "gemini": ("google", "gemini/"),
    "elevenlabs": ("elevenlabs", "elevenlabs/"),
    "bedrock": ("bedrock", ""),
    "bedrock_converse": ("bedrock", ""),
}
_AZURE_PLAIN = 2  # azure/<model>
_AZURE_QUALIFIED = 3  # azure/<zone or global alias>/<model>


def _azure_name(key: str) -> tuple[str, str] | None:
    """(model name, variant) for an Azure key, or None for keys we do not map."""
    parts = key.split("/")
    if parts[0] != "azure":
        return None
    if len(parts) == _AZURE_PLAIN:
        return parts[1], "base"
    if len(parts) == _AZURE_QUALIFIED and parts[1] in GLOBAL_ALIASES:
        return parts[2], "alt"
    if len(parts) == _AZURE_QUALIFIED and parts[1] in ZONES:
        return parts[2], "zone"
    return None


def map_key(key: str, spec: Spec) -> Mapped | None:
    """The catalog id LiteLLM entry `key` stands for, or None when it is out of scope."""
    mode = LITELLM_MODES.get(str(spec.get("mode")))
    provider = spec.get("litellm_provider")
    if mode is None or not isinstance(provider, str):
        return None
    slug, variant = "azure_openai", "base"
    if provider == "azure":
        named = _azure_name(key)
        if named is None:
            return None
        name, variant = named
    elif provider in _SIMPLE:
        slug, prefix = _SIMPLE[provider]
        name = key.removeprefix(prefix)
    else:
        return None
    if not name or "/" in name:
        return None
    return Mapped(key, f"{slug}/{name}", variant, mode, spec)


def convert_meters(slug: str, spec: Spec) -> dict[str, Decimal]:
    """LiteLLM per-token/second/character prices as our per-MTok/minute/1k-character meters."""
    found: dict[str, Decimal] = {}

    def put(meter: str, field_name: str, factor: Decimal) -> None:
        value = spec.get(field_name)
        if value is not None:
            found[meter] = scale(value, factor)

    cache_write = "cache_creation_input_token_cost"
    if slug in {"anthropic", "bedrock"} and spec.get(f"{cache_write}_above_1hr") is not None:
        cache_write += "_above_1hr"  # we store the dearer 1-hour price
    put("input_per_mtok", "input_cost_per_token", _MILLION)
    put("cached_input_per_mtok", "cache_read_input_token_cost", _MILLION)
    put("cache_write_per_mtok", cache_write, _MILLION)
    put("output_per_mtok", "output_cost_per_token", _MILLION)
    put("reasoning_per_mtok", "output_cost_per_reasoning_token", _MILLION)
    put("audio_input_per_mtok", "input_cost_per_audio_token", _MILLION)
    put("audio_output_per_mtok", "output_cost_per_audio_token", _MILLION)
    put("audio_input_per_minute", "input_cost_per_second", Decimal(60))
    put("per_1k_characters", "input_cost_per_character", Decimal(1000))
    return found


@dataclass(frozen=True, slots=True)
class LongContext:
    threshold: int
    input_multiplier: Decimal
    output_multiplier: Decimal


def long_contexts(spec: Spec) -> list[LongContext]:
    """Whole-request multipliers LiteLLM lists as `*_above_<N>k_tokens` input/output prices."""
    base_in = spec.get("input_cost_per_token")
    base_out = spec.get("output_cost_per_token")
    found = []
    for name in spec:
        m = _ABOVE.match(name)
        if m is None or base_in is None or to_decimal(base_in) == 0:
            continue
        in_mult = _CTX.divide(to_decimal(spec[name]), to_decimal(base_in))
        above_out = spec.get(f"output_cost_per_token_above_{m.group(1)}k_tokens")
        if above_out is not None and base_out is not None and to_decimal(base_out) != 0:
            out_mult = _CTX.divide(to_decimal(above_out), to_decimal(base_out))
        else:
            out_mult = Decimal(1)
        found.append(LongContext(int(m.group(1)) * 1000, in_mult, out_mult))
    return sorted(found, key=lambda lc: lc.threshold)


def index(data: Mapping[str, Spec]) -> dict[str, dict[str, list[Mapped]]]:
    """Mappable LiteLLM entries by our id, then by variant (key order is kept)."""
    found: dict[str, dict[str, list[Mapped]]] = defaultdict(lambda: defaultdict(list))
    for key, spec in data.items():
        if (m := map_key(key, spec)) is not None:
            found[m.id][m.variant].append(m)
    return found


def base_entry(variants: Mapping[str, list[Mapped]]) -> Mapped | None:
    for name in ("base", "alt"):
        if variants.get(name):
            return variants[name][0]
    return None


# --- drafting entries ------------------------------------------------------------------------


def _note(m: Mapped, source: Source, today: date) -> str:
    where = f"@ {source.commit[:12]}" if source.commit else f"(file sha256 {source.sha256[:12]})"
    return (
        f"From LiteLLM {m.key} {where} on {today}. Not checked: compare every "
        "price with the provider's page before setting verified: true."
    )


def _mode(m: Mapped, slug: str) -> str:
    if m.mode in {"chat", "responses"} and slug in {"anthropic", "bedrock"}:
        return "messages"
    if m.mode == "chat" and slug == "azure_openai":
        return "responses"
    return m.mode


def draft_entry(m: Mapped, source: Source, today: date) -> dict[str, Any]:
    """A catalog entry (not yet validated) drafted from LiteLLM entry `m`, `verified: false`."""
    slug = m.id.partition("/")[0]
    spec = m.spec
    entry: dict[str, Any] = {
        "id": m.id,
        "mode": _mode(m, slug),
        "currency": "USD",
        "effective_from": today.isoformat(),
        "source_url": source.url,
        "verified": False,
        "checked_on": today.isoformat(),
        "note": _note(m, source, today),
    }
    if slug == "azure_openai" or m.id.removeprefix("bedrock/").startswith("global."):
        entry["deployment_type"] = "global"
    conv = convert_meters(slug, spec)
    if conv.get("reasoning_per_mtok") == conv.get("output_per_mtok"):
        conv.pop("reasoning_per_mtok", None)  # priced as output already
    if m.mode == "embedding" and conv.get("output_per_mtok") == 0:
        del conv["output_per_mtok"]  # LiteLLM lists 0 for embeddings, which bill no output
    entry["meters"] = {k: dec_str(v) for k, v in conv.items()}
    listed = long_contexts(spec)
    if len(listed) == 1:
        lc = listed[0]
        entry["long_context"] = {
            "threshold_input_tokens": lc.threshold,
            "input_multiplier": dec_str(lc.input_multiplier),
            "output_multiplier": dec_str(lc.output_multiplier),
        }
    elif listed:
        tail = " Several long-context tiers exist: none imported."
        entry["note"] = entry["note"][: _NOTE_MAX - len(tail)] + tail
    limits = {
        name: whole(spec[field_name])
        for name, field_name in (
            ("max_output_tokens", "max_output_tokens"),
            ("context_window", "max_input_tokens"),
        )
        if spec.get(field_name) is not None
    }
    if limits:
        entry["limits"] = limits
    if spec.get("deprecation_date") is not None:
        entry["deprecation_date"] = str(spec["deprecation_date"])
    return entry


def entry_problem(entry: Mapping[str, Any]) -> str | None:
    """Why `entry` must not be drafted, or None. Never names a value.

    Stricter than the catalog schema: a zero meter or a long-context discount from a third
    party would make calls free or cheaper than billed, so they are refused here (M-10).
    """
    limits = entry.get("limits") or {}
    if entry.get("mode") in _CHAT_MODES and limits.get("max_output_tokens") is None:
        # The worst-case hold needs a ceiling; guessing one would let a call outspend its budget.
        return "no output ceiling (LiteLLM lists no max_output_tokens)"
    for meter, price in (entry.get("meters") or {}).items():
        if Decimal(price) == 0:
            return f"zero price for {meter}"
    lc = entry.get("long_context") or {}
    for name in ("input_multiplier", "output_multiplier"):
        if name in lc and Decimal(lc[name]) < 1:
            return f"long-context {name} below 1"
    try:
        PriceEntry.model_validate(dict(entry))
    except ValidationError as exc:
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<entry>'}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)
        )
    return None


@dataclass(slots=True)
class Drafts:
    entries: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (LiteLLM key, reason)
    carried: list[str] = field(default_factory=list)  # ids left alone: the catalog has them


def draft_overrides(
    source: Source, *, carried: Set[str], providers: Set[str], today: date
) -> Drafts:
    """Valid `verified: false` drafts for mappable models of `providers` not in `carried`.

    An id the catalog carries when the draft is made is left out: a newer unverified entry
    would replace a reviewed price from its date on. One bad entry is skipped, never fatal.
    """
    out = Drafts()
    for entry_id, variants in sorted(index(source.data).items()):
        m = base_entry(variants)
        if m is None or entry_id.partition("/")[0] not in providers:
            continue
        if entry_id in carried:
            out.carried.append(entry_id)
            continue
        try:
            entry = draft_entry(m, source, today)
        except (LiteLLMError, ArithmeticError):  # NaN, a string, a bool, an exponent overflow
            out.skipped.append((m.key, "a price or limit is not a usable finite number"))
            continue
        if (problem := entry_problem(entry)) is not None:
            out.skipped.append((m.key, problem))
            continue
        out.entries.append(entry)
    return out


HEADER = """\
# Drafted from LiteLLM's community price file on {today}: {origin},
# sha256 of the bytes read {sha}.
# Every entry is verified: false. LiteLLM is a third party: its numbers can be wrong, stale or
# changed by whoever controls that repository. spendgate prices calls with them as written, so
# a wrong or zero price here means a wrong or missing charge in your ledger. Compare each price
# with the provider's own page before relying on it, then set verified: true and checked_on.
"""


def header(source: Source, today: date) -> str:
    origin = f"{REPO} @ {source.commit}" if source.commit else "a local copy, commit not named"
    return HEADER.format(origin=origin, sha=source.sha256, today=today)
