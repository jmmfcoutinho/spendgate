"""Parse and resolve the price catalog from already-loaded dicts.

`parse` takes the bundled document first and override documents after it, as plain dicts, plus the
sha256 of the files they came from; the caller (config) reads the files. The catalog can never
fail open: unknown keys, a zero-charge option or out-of-range `free_statuses` are load errors.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.money import FxSnapshot, Money, convert_to
from spendgate.core.outcomes import provider_free_statuses, validate_free_statuses
from spendgate.core.price_regions import Region, can_price, overlap, pick, split

PROVIDERS = (
    "openai", "azure_openai", "anthropic", "bedrock", "google", "azure_speech", "elevenlabs",
)  # fmt: skip
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Same patterns as schema.json, so both validators accept exactly the same strings. Pydantic's
# `pattern` is unanchored, hence the explicit ^...$ where one is used as a field pattern.
_DECIMAL = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]+)?")
_MULTIPLIER = re.compile(r"(0\.[0-9]*[1-9][0-9]*|[1-9][0-9]*(\.[0-9]+)?)")
_ID = re.compile(r"[a-z][a-z0-9_]*/[A-Za-z0-9][A-Za-z0-9._:/-]*")
_REGION = re.compile(r"[a-z0-9-]+")
_CHAT_MODES = ("chat", "responses", "messages")
# Meters that bill reply tokens; the hold prices the output ceiling at the dearest of them.
OUTPUT_METERS = ("output_per_mtok", "reasoning_per_mtok", "audio_output_per_mtok")


def _decimal_string(pattern: re.Pattern[str]) -> BeforeValidator:
    def check(value: object) -> Decimal:
        # YAML floats are rejected on purpose: 0.1 as a float is not 0.1.
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError("must be a quoted decimal string such as '0.012'")
        return Decimal(value)

    return BeforeValidator(check)


def _iso_date(value: object) -> date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError("must be a quoted YYYY-MM-DD date")
    return date.fromisoformat(value)


Price = Annotated[Decimal, _decimal_string(_DECIMAL)]
Multiplier = Annotated[Decimal, _decimal_string(_MULTIPLIER)]
IsoDate = Annotated[date, BeforeValidator(_iso_date)]
PosInt = Annotated[int, Field(strict=True, ge=1)]
Text = Annotated[str, Field(strict=True)]
RegionSlug = Annotated[str, Field(strict=True, pattern=f"^{_REGION.pattern}$")]


def is_catalog_id(text: str) -> bool:
    """`provider/model` as an entry's `id` must spell it (the provider is not checked here)."""
    return _ID.fullmatch(text) is not None


def is_region_slug(text: str) -> bool:
    """A Region as `regions` and `region` must spell it (`swedencentral`, `eu-west-1`)."""
    return _REGION.fullmatch(text) is not None


class CatalogError(ValueError):
    """The catalog documents are invalid; nothing is served from them."""


class ModelNotPriced(LookupError):
    """No current price entry for a model: the call must be refused before any upstream contact."""

    def __init__(self, provider: str, model: str, reason: str) -> None:
        super().__init__(f"{provider}/{model}: {reason}")
        self.provider = provider
        self.model = model
        self.reason = reason

    def refusal(self) -> Refusal:
        """The protocol refusal: no price, no call (ADR-0028), and where an operator adds one."""
        catalog_id = f"{self.provider}/{self.model}"
        return Refusal(
            ErrorCode.MODEL_NOT_PRICED,
            f"{self}. An operator can price it in a catalog override file (catalog.paths), or "
            f"draft a stand-in with `spendgate catalog draft --like <priced id> --as {catalog_id} "
            "--out <file>`.",
            param="model",
        )


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Meters(_Strict):
    """Per-unit prices; the unit is in the field name."""

    input_per_mtok: Price | None = None
    output_per_mtok: Price | None = None
    cached_input_per_mtok: Price | None = None
    cache_write_per_mtok: Price | None = None
    reasoning_per_mtok: Price | None = None
    audio_input_per_mtok: Price | None = None
    cached_audio_input_per_mtok: Price | None = None
    audio_output_per_mtok: Price | None = None
    audio_input_per_minute: Price | None = None
    audio_granularity_seconds: PosInt = 1
    per_1k_characters: Price | None = None
    per_image: Price | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("meters needs at least one price")
        return self


class LongContext(_Strict):
    """Whole-request multipliers once `input_tokens` strictly exceed the threshold."""

    threshold_input_tokens: PosInt
    input_multiplier: Multiplier
    output_multiplier: Multiplier


class Limits(_Strict):
    max_output_tokens: PosInt | None = None
    context_window: PosInt | None = None
    # Input tokens held per image part; unset means the estimator's conservative default.
    max_image_tokens: PosInt | None = None


class PriceEntry(_Strict):
    """One priced model version."""

    id: Annotated[str, Field(strict=True, pattern=f"^{_ID.pattern}$")]
    mode: Literal["chat", "responses", "messages", "embedding", "transcription", "speech"]
    currency: Literal["USD", "EUR"]
    effective_from: IsoDate
    source_url: Annotated[str, Field(strict=True, pattern=r"^https://")]
    verified: Annotated[bool, Field(strict=True)]
    checked_on: IsoDate | None = None
    note: Annotated[str, Field(strict=True, max_length=300)] | None = None
    deployment_type: (
        Literal[
            "global",
            "data_zone",
            "regional",
            "standard",
            "batch",
            "flex",
            "priority",
            "provisioned",
        ]
        | None
    ) = None
    region: RegionSlug | None = None  # documentation only; `regions` is enforced
    regions: Annotated[tuple[RegionSlug, ...], Field(min_length=1)] | None = None
    price_multiplier: Multiplier = Decimal(1)
    meters: Meters
    long_context: LongContext | None = None
    limits: Limits | None = None
    deprecation_date: IsoDate | None = None

    @property
    def provider(self) -> str:
        return self.id.partition("/")[0]

    @property
    def model(self) -> str:
        return self.id.partition("/")[2]

    @property
    def price_id(self) -> str:
        """The `(id, effective_from)` pair; `price_id_in` is what a call row records."""
        return f"{self.id}@{self.effective_from.isoformat()}"

    @property
    def key(self) -> tuple[str, date, frozenset[str] | None]:
        """What overrides merge by: one entry per id, date and Region set."""
        return (self.id, self.effective_from, frozenset(self.regions or ()) or None)

    def price_id_in(self, region: Region) -> str:
        """The price id recorded on a call row: `~<region>` when a Region-scoped entry priced it.

        Region sets of one id and date are disjoint, so the suffix names exactly one entry.
        """
        if self.regions is not None and region in self.regions:
            return f"{self.price_id}~{region}"
        return self.price_id

    def described(self) -> str:
        """`price_id` plus the Regions it is scoped to, for messages."""
        return f"{self.price_id}~{','.join(self.regions)}" if self.regions else self.price_id

    @model_validator(mode="after")
    def _regions_unique(self) -> Self:
        if self.regions is not None and len(set(self.regions)) != len(self.regions):
            raise ValueError("regions must not repeat a region")
        return self

    @model_validator(mode="after")
    def _mode_rules(self) -> Self:
        if self.provider not in PROVIDERS:
            raise ValueError(f"provider {self.provider!r} is not one of {', '.join(PROVIDERS)}")
        m = self.meters
        if self.mode in _CHAT_MODES:
            if m.input_per_mtok is None or m.output_per_mtok is None:
                raise ValueError(f"{self.mode} needs input_per_mtok and output_per_mtok")
            if self.limits is None or self.limits.max_output_tokens is None:
                raise ValueError(f"{self.mode} needs limits.max_output_tokens")
        elif self.mode == "embedding" and m.input_per_mtok is None:
            raise ValueError("embedding needs input_per_mtok")
        elif self.mode == "transcription":
            self._transcription_rules()
        elif self.mode == "speech" and (
            m.per_1k_characters is None and m.audio_output_per_mtok is None
        ):
            raise ValueError("speech needs per_1k_characters or audio_output_per_mtok")
        return self

    def _transcription_rules(self) -> None:
        m = self.meters
        if m.audio_input_per_minute is None and m.audio_input_per_mtok is None:
            raise ValueError("transcription needs audio_input_per_minute or audio_input_per_mtok")
        # No request field bounds a transcript, so its hold rests on this limit alone (M-19).
        billed = any(getattr(m, name) is not None for name in OUTPUT_METERS)
        if billed and (self.limits is None or self.limits.max_output_tokens is None):
            raise ValueError("transcription with an output meter needs limits.max_output_tokens")


class _Settlement(_Strict):
    free_statuses: tuple[Annotated[int, Field(strict=True)], ...] | None = None

    @model_validator(mode="after")
    def _only_throttling(self) -> Self:
        if self.free_statuses is not None:
            if len(set(self.free_statuses)) != len(self.free_statuses):
                raise ValueError("free_statuses must not repeat a status")
            validate_free_statuses(self.free_statuses)
        return self


class _Document(_Strict):
    catalog_version: Literal[1]
    updated: IsoDate | None = None
    providers: dict[Text, _Settlement] = Field(default_factory=dict)
    models: Annotated[list[PriceEntry], Field(min_length=1)]

    @model_validator(mode="after")
    def _providers_known(self) -> Self:
        for slug in self.providers:
            if slug not in PROVIDERS:
                raise ValueError(f"unknown provider {slug!r} in providers")
        return self


def _validate(raw: Mapping[str, object], index: int) -> _Document:
    try:
        doc = _Document.model_validate(dict(raw))
    except ValidationError as exc:
        found = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<document>'}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)
        )
        raise CatalogError(f"catalog document {index}: {found}") from exc
    seen: set[tuple[str, date, frozenset[str] | None]] = set()
    for entry in doc.models:
        if entry.key in seen:
            raise CatalogError(f"catalog document {index}: duplicate {entry.described()}")
        seen.add(entry.key)
    if (problem := overlap(doc.models)) is not None:
        raise CatalogError(f"catalog document {index}: {problem}")
    return doc


@dataclass(frozen=True, slots=True)
class Catalog:
    """Resolved prices. Lookup picks the effective entry by date at call time."""

    sha256: str  # recorded on every call row (T-CAT-05)
    ledger_currency: str
    fx: tuple[FxSnapshot, ...]
    _by_model: Mapping[tuple[str, str], tuple[PriceEntry, ...]]  # newest effective_from first
    _free: Mapping[str, frozenset[int]]
    _replaced_verified: frozenset[str] = frozenset()  # unverified price_ids that replaced one

    def lookup(self, provider: str, model: str, now: int, region: Region = None) -> PriceEntry:
        """Entry in force at `now` (epoch ms, UTC date) for a deployment in `region`.

        `ModelNotPriced` when none is. A deprecated winner never falls back to an older entry.
        """
        entry = pick(self._by_model.get((provider, model), ()), region, _day(now))
        if entry is None:
            raise ModelNotPriced(provider, model, "no price entry in force")
        if entry.deprecation_date is not None and entry.deprecation_date <= _day(now):
            raise ModelNotPriced(provider, model, "model is past its deprecation date")
        return entry

    def lookup_route(
        self, provider: str, model: str, regions: Sequence[Region], now: int
    ) -> PriceEntry:
        """The one entry that prices every deployment Region of a route (ADR-0028, point 4).

        Load refuses a route whose Regions resolve differently; refusing here too keeps a hold
        from ever being sized with one Region's price for another.
        """
        entries = [self.lookup(provider, model, now, r) for r in regions or (None,)]
        if len({e.key for e in entries}) > 1:
            raise ModelNotPriced(provider, model, "deployments resolve to different prices")
        return entries[0]

    def listed_regions(self, provider: str, model: str) -> tuple[str, ...]:
        """Every Region some entry of the id is scoped to, sorted; empty for region-less ids."""
        entries = self._by_model.get((provider, model), ())
        return tuple(sorted({r for e in entries for r in e.regions or ()}))

    def could_price(self, provider: str, model: str, region: Region) -> bool:
        """Whether some entry, on some date, prices a deployment in `region`."""
        return can_price(self._by_model.get((provider, model), ()), region)

    def split_by_region(
        self, provider: str, model: str, regions: Sequence[Region], now: int | None
    ) -> Mapping[Region, PriceEntry] | None:
        """Regions that resolve to different entries, from `now` on (any date if None)."""
        today = None if now is None else _day(now)
        return split(self._by_model.get((provider, model), ()), regions, today)

    def free_statuses(self, provider: str) -> frozenset[int]:
        """Statuses that mean 'rejected before processing' for this provider."""
        if provider in self._free:  # an explicit empty list stays empty (nothing is free)
            return self._free[provider]
        return provider_free_statuses(provider)

    def in_ledger(self, amount: Money) -> Money:
        """Convert a catalog-currency cost to the ledger currency (one ceiling, snapshot rate)."""
        return convert_to(amount, self.ledger_currency, self.fx)

    def entries(self) -> tuple[PriceEntry, ...]:
        return tuple(e for group in self._by_model.values() for e in group)

    def unverified_over_verified(self) -> tuple[str, ...]:
        """Unverified entries that supersede a verified entry of the same id, as price_ids.

        Either a later `effective_from` or the same one in a later document. Not an error,
        but worth a warning: from that date a reviewed price gives way to an unchecked one.
        """
        found = set(self._replaced_verified)
        for group in self._by_model.values():
            for entry in group:
                older = (e for e in group if e.effective_from < entry.effective_from)
                if not entry.verified and any(e.verified for e in older):
                    found.add(entry.price_id)
        return tuple(sorted(found))


def _day(now: int) -> date:
    return datetime.fromtimestamp(now // 1000, tz=UTC).date()


def parse(
    documents: Sequence[Mapping[str, object]],
    sha256: str,
    *,
    ledger_currency: str,
    fx: Sequence[FxSnapshot] = (),
) -> Catalog:
    """Validate the bundled document and overrides, merge them and require FX for every currency.

    Later documents win per `(id, effective_from, regions)` and per provider block; Region sets
    of one id and date that overlap are refused. Raises `CatalogError` for invalid data and
    `MissingFxRate` when a currency cannot reach the ledger currency.
    """
    if not _SHA256.fullmatch(sha256):
        raise CatalogError("sha256 must be 64 lower-case hex characters")
    merged: dict[tuple[str, date, frozenset[str] | None], PriceEntry] = {}
    free: dict[str, frozenset[int]] = {}
    replaced: set[str] = set()
    for index, raw in enumerate(documents):
        doc = _validate(raw, index)
        for e in doc.models:
            before = merged.get(e.key)
            if before is not None and before.verified and not e.verified:
                replaced.add(e.price_id)
            merged[e.key] = e
        for slug, rule in doc.providers.items():
            rules: dict[str, object] = {}
            if rule.free_statuses is not None:
                rules["free_statuses"] = list(rule.free_statuses)
            free[slug] = provider_free_statuses(slug, rules)  # the model already validated it
    if not merged:
        raise CatalogError("no catalog documents given")
    if (problem := overlap(merged.values())) is not None:
        raise CatalogError(f"catalog documents overlap: {problem}")
    for currency in sorted({e.currency for e in merged.values()}):
        convert_to(Money.zero(currency), ledger_currency, fx)  # raises MissingFxRate
    return Catalog(
        sha256,
        ledger_currency,
        tuple(fx),
        MappingProxyType(_by_model(merged.values())),
        MappingProxyType(free),
        frozenset(replaced),
    )


def _by_model(entries: Iterable[PriceEntry]) -> dict[tuple[str, str], tuple[PriceEntry, ...]]:
    """Entries per (provider, model), newest `effective_from` first."""
    grouped: dict[tuple[str, str], list[PriceEntry]] = {}
    for entry in entries:
        grouped.setdefault((entry.provider, entry.model), []).append(entry)
    return {
        key: tuple(sorted(group, key=lambda e: e.effective_from, reverse=True))
        for key, group in grouped.items()
    }
