"""What loading spendgate.yaml produces: typed, validated settings with secrets resolved.

`config.load` builds these; everything else only reads them. Secret values stay in `Secret`
holders, hidden from reprs and messages.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from spendgate.config.schema import Config
from spendgate.config.secrets import Secret
from spendgate.core.catalog import Catalog, PriceEntry
from spendgate.core.health import HealthConfig
from spendgate.core.money import FxSnapshot
from spendgate.core.policy import model_filter_matches

_MS = 1000


@dataclass(frozen=True, slots=True)
class ResolvedDeployment:
    """A routable target with its credential resolved (hidden from reprs)."""

    provider: str
    base_url: str
    model: str
    auth: str
    region: str | None
    api_version: str | None
    credential: Secret | None
    ambient: bool = False  # `credential: adc`: the host's own cloud identity, no stored secret


def is_vertex(provider: str, auth: str) -> bool:
    """A Google Vertex AI deployment: the `google` adapter with bearer-token auth."""
    return provider == "google" and auth == "bearer_token"


@dataclass(frozen=True, slots=True)
class Route:
    alias: str
    catalog_id: str
    deployments: tuple[ResolvedDeployment, ...]
    # Priced only by entries that list each deployment's Region, never by a region-less one: a
    # route with a Vertex AI deployment, whose region-less entries are Gemini API prices (ADR-0034).
    listed_only: bool = False

    def price(self, catalog: Catalog, now: int) -> PriceEntry:
        """The one entry that prices every deployment at `now` (ADR-0028 point 4), or raises
        `ModelNotPriced`."""
        provider, _, model = self.catalog_id.partition("/")
        regions = [d.region for d in self.deployments]
        return catalog.lookup_route(provider, model, regions, now, listed_only=self.listed_only)


@dataclass(frozen=True, slots=True)
class WebhookTarget:
    """`alerts.webhook` with its URL and signing secret resolved (both hidden from reprs)."""

    url: Secret  # a webhook URL often carries a token: it is never logged
    signing_secret: Secret
    timeout_seconds: int
    max_attempts: int
    queue_size: int


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the gateway needs, validated. Secrets are `Secret` holders."""

    source: Path
    config: Config
    catalog: Catalog
    catalog_documents: tuple[Mapping[str, object], ...]
    catalog_sha256: str  # combined hash of every catalog file, recorded on calls (T-CAT-05)
    fx: tuple[FxSnapshot, ...]
    key_pepper: Secret
    ledger_dsn: Secret | None  # postgres only
    ledger_path: Path | None  # sqlite only, resolved against the config file's directory
    routes: Mapping[str, Route]
    alert_webhook: WebhookTarget | None = None  # `spendgate serve` posts budget warnings here

    @property
    def ledger_currency(self) -> str:
        return self.config.ledger.currency

    @property
    def lease_ms(self) -> int:
        return self.config.ledger.lease_seconds * _MS

    @property
    def sweep_ms(self) -> int:
        return self.config.ledger.sweep_seconds * _MS

    @property
    def heartbeat_ms(self) -> int:
        beat = self.config.ledger.heartbeat_seconds
        return beat * _MS if beat is not None else self.lease_ms // 3

    @property
    def health(self) -> HealthConfig:
        r = self.config.routing
        return HealthConfig(
            cooldown_ms=r.cooldown_seconds * _MS,
            failure_threshold_pct=r.failure_threshold_pct,
            min_requests=r.min_requests,
            window_ms=r.window_seconds * _MS,
            max_cooldown_ms=r.max_cooldown_seconds * _MS,
        )

    def fail_open_summary(self) -> list[str]:
        """Lines the gateway logs (as warnings) at start: every enabled opt-out, or none."""
        lines = []
        if self.config.fail_open.allow_remote_media:
            lines.append(
                "fail_open.allow_remote_media is ON: URLs in request bodies are accepted and "
                "fetched by the provider (SSRF exposure)"
            )
        return lines or ["fail_open: no opt-outs enabled"]

    def unverified_summary(self) -> str | None:
        """One warning line naming the catalog entries with `verified: false`, or None."""
        return unverified_summary(self.catalog)

    def superseded_summary(self) -> str | None:
        """One warning line when an unverified entry supersedes a verified one, or None."""
        ids = self.catalog.unverified_over_verified()
        if not ids:
            return None
        more = f" and {len(ids) - _IDS_SHOWN} more" if len(ids) > _IDS_SHOWN else ""
        verb = "entry supersedes" if len(ids) == 1 else "entries supersede"
        return (
            f"{len(ids)} unverified catalog {verb} a verified price of the same id: "
            f"{', '.join(ids[:_IDS_SHOWN])}{more}"
        )

    def unlisted_regions(self) -> list[str]:
        """A warning per routed deployment Region that no entry of its Region-scoped id lists.

        Not an error: such a Region is priced by the region-less entry, which may be right. But
        a Region spelled differently from the catalog (`swedencental`) would get that price too,
        so it is named at start with the Regions the catalog does list. A Vertex AI route
        never falls back (ADR-0034): load refuses its unlisted Regions instead.
        """
        lines: dict[str, None] = {}
        for alias, route in self.routes.items():
            provider, _, model = route.catalog_id.partition("/")
            listed = self.catalog.listed_regions(provider, model)
            if not listed or route.listed_only:
                continue
            shown = ", ".join(listed[:_REGIONS_SHOWN])
            more = (
                f" and {len(listed) - _REGIONS_SHOWN} more" if len(listed) > _REGIONS_SHOWN else ""
            )
            for region in (d.region for d in route.deployments):
                if region is not None and region not in listed:
                    line = (
                        f"models.{alias} region {region!r} is listed by no entry of "
                        f"{route.catalog_id}, so it takes the region-less price (Regions listed "
                        f"for that id: {shown}{more})"
                    )
                    lines[line] = None
        return list(lines)

    def unmatched_filters(self) -> list[str]:
        """A warning per bootstrap budget whose `model_filter` matches no routed catalog id.

        Not an error: the routes may grow into the filter later. Until then the budget takes
        part in no hold; the usual cause is a filter written against an alias.
        """
        routed = sorted({r.catalog_id for r in self.routes.values()})
        shown = ", ".join(routed[:_IDS_SHOWN])
        return [
            f"bootstrap.budgets.{i}.model_filter {b.model_filter!r} matches no routed catalog "
            f"id, so the budget never applies (filters match catalog ids such as {shown}, "
            "not aliases)"
            for i, b in enumerate(self.config.bootstrap.budgets)
            if b.model_filter is not None
            and not any(model_filter_matches(b.model_filter, m) for m in routed)
        ]


_IDS_SHOWN = 5
_REGIONS_SHOWN = 10


def unverified_summary(catalog: Catalog) -> str | None:
    """A warning naming how many effective entries have `verified: false` and the first few ids."""
    ids = sorted({e.price_id for e in catalog.entries() if not e.verified})
    if not ids:
        return None
    shown = ", ".join(ids[:_IDS_SHOWN])
    more = f" and {len(ids) - _IDS_SHOWN} more" if len(ids) > _IDS_SHOWN else ""
    noun = "entry has" if len(ids) == 1 else "entries have"
    return (
        f"{len(ids)} catalog {noun} unverified prices (verified: false; not compared with the "
        f"provider's page): {shown}{more}"
    )


@dataclass(frozen=True, slots=True)
class CheckReport:
    """Result of `check_config`: nothing is started."""

    errors: tuple[str, ...]
    unpriced: tuple[str, ...]  # routed catalog ids without a price
    unverified: str | None = None  # warning line when the catalog has unverified prices
    unmatched_filters: tuple[str, ...] = ()  # bootstrap budgets whose filter matches no route
    superseded: str | None = None  # warning line when unverified entries replace verified ones
    unlisted_regions: tuple[str, ...] = ()  # deployment Regions their Region-scoped id never lists

    @property
    def ok(self) -> bool:
        return not self.errors
