"""A small Prometheus registry and text-format 0.0.4 renderer (no client library).

Why hand-rolled: the base install has a fixed dependency budget and we need only counters,
gauges and fixed-bucket histograms.

Label cardinality is bounded by design: only model, deployment, scope, outcome, token kind,
webhook result, catalog sha, ledger operation and heartbeat step, and every value comes from
config, the ledger or a fixed set in the code, never from the request. `model` is a routed
catalog id, or `unknown` for a refusal that names no routed model; `deployment` is the config
label `<alias>/<index>`; `scope` is an admin-made scope path. Never the raw body model, key
secrets or prefixes, user labels, session ids, tags or request ids: each distinct value would
mint a new time series forever (memory growth on the scraper) and would leak identifiers into a
system with weaker access control than the ledger.
"""

import contextlib
import math
from collections.abc import Callable, Mapping, Sequence

LATENCY_BUCKETS_MS: tuple[float, ...] = (
    10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000, 30_000, 60_000, 120_000,
)  # fmt: skip
TTFT_BUCKETS_MS: tuple[float, ...] = (50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000, 30_000)
# Work the gateway does itself (ledger calls, pool waits, overhead): mostly a few ms; 10 s is the
# hold deadline.
FAST_BUCKETS_MS: tuple[float, ...] = (
    1, 2.5, 5, 10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000,
)  # fmt: skip

type LabelKey = tuple[tuple[str, str], ...]


def escape_label_value(value: str) -> str:
    """Escape backslash, double quote and newline as the text format requires."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _fmt(value: float) -> str:
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if math.isnan(value):
        return "NaN"
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _labels(key: LabelKey, extra: tuple[tuple[str, str], ...] = ()) -> str:
    pairs = key + extra
    if not pairs:
        return ""
    return "{" + ",".join(f'{k}="{escape_label_value(v)}"' for k, v in pairs) + "}"


class _Metric:
    kind = ""

    def __init__(self, name: str, help_text: str, label_names: Sequence[str]) -> None:
        self.name = name
        self.help = help_text
        self.label_names = tuple(label_names)

    def _key(self, labels: Mapping[str, str]) -> LabelKey:
        # A wrong label set is a programming error; fail loudly rather than mint a series.
        if set(labels) != set(self.label_names):
            raise ValueError(f"{self.name}: labels must be exactly {self.label_names}")
        return tuple((n, labels[n]) for n in self.label_names)

    def _header(self) -> list[str]:
        return [f"# HELP {self.name} {_escape_help(self.help)}", f"# TYPE {self.name} {self.kind}"]

    def lines(self) -> list[str]:
        raise NotImplementedError


class Counter(_Metric):
    kind = "counter"

    def __init__(self, name: str, help_text: str, label_names: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, label_names)
        self._values: dict[LabelKey, float] = {}

    def inc(self, amount: float = 1, **labels: str) -> None:
        if amount < 0:
            raise ValueError("a counter never decreases")
        key = self._key(labels)
        self._values[key] = self._values.get(key, 0) + amount

    def value(self, **labels: str) -> float:
        return self._values.get(self._key(labels), 0)

    def lines(self) -> list[str]:
        out = self._header()
        out += [f"{self.name}{_labels(k)} {_fmt(v)}" for k, v in sorted(self._values.items())]
        return out


class Gauge(Counter):
    kind = "gauge"

    def inc(self, amount: float = 1, **labels: str) -> None:  # gauges may move both ways
        key = self._key(labels)
        self._values[key] = self._values.get(key, 0) + amount

    def set(self, value: float, **labels: str) -> None:
        self._values[self._key(labels)] = value

    def remove(self, **labels: str) -> None:
        """Drop a series (a scope that no longer exists must not report a stale value)."""
        self._values.pop(self._key(labels), None)


class Histogram(_Metric):
    kind = "histogram"

    def __init__(
        self, name: str, help_text: str, buckets: Sequence[float], label_names: Sequence[str] = ()
    ) -> None:
        super().__init__(name, help_text, label_names)
        self.buckets = tuple(sorted(buckets))
        self._counts: dict[LabelKey, list[int]] = {}
        self._sums: dict[LabelKey, float] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        counts = self._counts.setdefault(key, [0] * (len(self.buckets) + 1))
        for i, bound in enumerate(self.buckets):
            if value <= bound:
                counts[i] += 1
                break
        else:
            counts[-1] += 1
        self._sums[key] = self._sums.get(key, 0) + value

    def count(self, **labels: str) -> int:
        return sum(self._counts.get(self._key(labels), []))

    def lines(self) -> list[str]:
        out = self._header()
        for key, counts in sorted(self._counts.items()):
            running = 0
            for bound, n in zip(self.buckets, counts, strict=False):
                running += n
                out.append(f"{self.name}_bucket{_labels(key, (('le', _fmt(bound)),))} {running}")
            running += counts[-1]
            out.append(f"{self.name}_bucket{_labels(key, (('le', '+Inf'),))} {running}")
            out.append(f"{self.name}_sum{_labels(key)} {_fmt(self._sums[key])}")
            out.append(f"{self.name}_count{_labels(key)} {running}")
        return out


# (attribute, kind, metric name, help, labels). One row per series keeps `Metrics.__init__` short.
_COUNTERS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "requests",
        "spendgate_requests_total",
        "Requests by outcome and model.",
        ("outcome", "model"),
    ),
    (
        "spend_nano",
        "spendgate_spend_nano_total",
        "Settled spend in nano-units.",
        ("model", "scope"),
    ),
    ("tokens", "spendgate_tokens_total", "Tokens by kind (input, output, cached).", ("kind",)),
    ("throttles", "spendgate_throttles_total", "Upstream 429s.", ("model", "deployment")),
    ("cooldowns", "spendgate_cooldowns_total", "Deployment cooldowns started.", ("deployment",)),
    ("overruns", "spendgate_overruns_total", "Settlements above the hold.", ("scope",)),
    (
        "review_required",
        "spendgate_usage_review_required_total",
        "Settlements flagged for review (absurd reported usage, booked in full).",
        ("model",),
    ),
    (
        "alert_webhooks",
        "spendgate_alert_webhooks_total",
        "Budget-warning webhook posts by result (delivered, failed, dropped).",
        ("result",),
    ),
    (
        "ledger_errors",
        "spendgate_ledger_errors_total",
        "Ledger calls that failed (raised, or a hold past its deadline), by operation.",
        ("op",),
    ),
    (
        "ledger_unavailable",
        "spendgate_ledger_unavailable_total",
        "Requests refused as ledger_unavailable (key check or hold); nothing was sent.",
        (),
    ),
    (
        "sweeper_expired",
        "spendgate_sweeper_expired_total",
        "Expired holds the sweeper charged in full.",
        (),
    ),
    (
        "heartbeat_failures",
        "spendgate_heartbeat_failures_total",
        "Heartbeat steps that raised (retried at the next beat), by step.",
        ("step",),
    ),
)
_GAUGES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "budget_remaining_nano",
        "spendgate_budget_remaining_nano",
        "Remaining budget in nano-units.",
        ("scope",),
    ),
    ("holds_in_flight", "spendgate_holds_in_flight", "Pending reservations (holds) right now.", ()),
    ("catalog_age", "spendgate_catalog_age_seconds", "Seconds since the catalog was updated.", ()),
    ("catalog_info", "spendgate_catalog_info", "Always 1; the catalog sha is the label.", ("sha",)),
    (
        "unsettled_closes",
        "spendgate_unsettled_closes",
        "Closes the ledger has not taken yet: retried each heartbeat, else swept in full.",
        (),
    ),
    ("pool_size", "spendgate_ledger_pool_size", "Postgres pool connections, idle or in use.", ()),
    ("pool_in_use", "spendgate_ledger_pool_in_use", "Postgres pool connections in use.", ()),
    ("pool_max", "spendgate_ledger_pool_max", "Most connections the Postgres pool opens.", ()),
)
_MODEL_DEPLOYMENT = ("model", "deployment")
_HISTOGRAMS: tuple[tuple[str, str, str, tuple[float, ...], tuple[str, ...]], ...] = (
    (
        "upstream_latency",
        "spendgate_upstream_latency_ms",
        "Upstream call latency in ms.",
        LATENCY_BUCKETS_MS,
        _MODEL_DEPLOYMENT,
    ),
    (
        "ttft",
        "spendgate_time_to_first_token_ms",
        "Streaming time to first token in ms.",
        TTFT_BUCKETS_MS,
        _MODEL_DEPLOYMENT,
    ),
    (
        "ledger_latency",
        "spendgate_ledger_latency_ms",
        "Ledger hold, settle, release and lease renewal latency in ms.",
        FAST_BUCKETS_MS,
        ("op",),
    ),
    (
        "pool_wait",
        "spendgate_ledger_pool_wait_ms",
        "Time a ledger transaction queued for a Postgres pool connection, in ms.",
        FAST_BUCKETS_MS,
        (),
    ),
    (
        "overhead",
        "spendgate_gateway_overhead_ms",
        "Time a held call spent outside the upstream call (hold, settle, routing), in ms.",
        FAST_BUCKETS_MS,
        (),
    ),
)


class Metrics:
    """All spendgate metrics. One instance is created at start-up and passed around."""

    requests: Counter
    spend_nano: Counter
    tokens: Counter
    throttles: Counter
    cooldowns: Counter
    overruns: Counter
    review_required: Counter
    alert_webhooks: Counter
    ledger_errors: Counter
    ledger_unavailable: Counter
    sweeper_expired: Counter
    heartbeat_failures: Counter
    budget_remaining_nano: Gauge
    holds_in_flight: Gauge
    catalog_age: Gauge
    catalog_info: Gauge
    unsettled_closes: Gauge
    pool_size: Gauge
    pool_in_use: Gauge
    pool_max: Gauge
    upstream_latency: Histogram
    ttft: Histogram
    ledger_latency: Histogram
    pool_wait: Histogram
    overhead: Histogram

    def __init__(self) -> None:
        self._all: list[_Metric] = []
        # Called at the start of every render: keeps time-based gauges (catalog age) fresh.
        self.before_render: Callable[[], None] | None = None
        self._readers: list[Callable[[], None]] = []
        for attr, name, text, labels in _COUNTERS:
            self._register(attr, Counter(name, text, labels))
        for attr, name, text, labels in _GAUGES:
            self._register(attr, Gauge(name, text, labels))
        for attr, name, text, buckets, labels in _HISTOGRAMS:
            self._register(attr, Histogram(name, text, buckets, labels))
        # A scrape before the first event should still see these (an alert on "absent" is noise).
        self.holds_in_flight.set(0)
        self.unsettled_closes.set(0)
        self.ledger_unavailable.inc(0)
        self.sweeper_expired.inc(0)

    def _register(self, attr: str, metric: _Metric) -> None:
        self._all.append(metric)
        setattr(self, attr, metric)

    def record_spend(self, *, nano: int, model: str, scope: str) -> None:
        self.spend_nano.inc(nano, model=model, scope=scope)

    def set_remaining(self, *, scope: str, nano: int) -> None:
        self.budget_remaining_nano.set(nano, scope=scope)

    def hold_opened(self) -> None:
        self.holds_in_flight.inc()

    def hold_closed(self) -> None:
        self.holds_in_flight.inc(-1)

    def record_cooldown(self, *, deployment: str) -> None:
        self.cooldowns.inc(deployment=deployment)

    def record_overrun(self, *, scope: str) -> None:
        self.overruns.inc(scope=scope)

    def set_catalog(self, *, sha: str, age_seconds: float | None) -> None:
        """Replace the info series so a reloaded catalog does not leave the old sha at 1.

        An unknown age (None) publishes no age series at all: 0 would read as "fresh" (CR-07).
        """
        self.catalog_info._values.clear()
        self.catalog_info.set(1, sha=sha)
        self.catalog_age.remove()
        if age_seconds is not None:
            self.catalog_age.set(age_seconds)

    def on_render(self, read: Callable[[], None]) -> None:
        """Run `read` before every scrape, to refresh gauges sampled from elsewhere (the pool).

        A reader that raises is skipped for that scrape: the rest of `/metrics` still answers.
        """
        self._readers.append(read)

    def render(self) -> str:
        """Prometheus text exposition format 0.0.4."""
        if self.before_render is not None:
            self.before_render()
        for read in self._readers:
            with contextlib.suppress(Exception):
                read()
        lines: list[str] = []
        for metric in self._all:
            lines += metric.lines()
        return "\n".join(lines) + "\n"
