# ADR-0019: Ledger health metrics and an optional OpenTelemetry extra

**Status:** Accepted
**Date:** 2026-11-24

## Context

Issue #34. `/metrics` said how much was spent and how calls ended, but not how healthy the money
path was: how long holds and settlements took, how often the ledger failed, whether the Postgres
pool was saturated, how many expired holds the sweeper charged in full, how many closes were
waiting for a retry, whether the supervised heartbeat (0.1.3) kept failing, and how much time the
gateway itself added to a call. Operators who run a tracing backend also asked to see a call's
hold, upstream call and settlement inside their own traces.

Two constraints shape the answer. The base install has a fixed dependency budget (at most six,
five used; CONTRIBUTING.md section 6), so no Prometheus or OpenTelemetry library may become a
runtime dependency. And metrics and traces leave the ledger's access control: they must never
carry prompts, keys, session ids, user or tag labels, and metric labels must stay bounded in
cardinality (issue #17).

## Decision

**Metrics** (the hand-rolled registry in `gateway/metrics.py`; no new dependency):

| Metric | Labels | Fed from |
|---|---|---|
| `spendgate_ledger_latency_ms` (histogram) | `op`: `hold`, `settle`, `release`, `renew` | each ledger call of the money path (`LedgerEvent`) |
| `spendgate_ledger_errors_total` | `op`: the above plus `sweep`, `retention` | a call that raised; a hold past its 10 s deadline counts |
| `spendgate_ledger_unavailable_total` | none | data-plane refusals with `ledger_unavailable` (key check or hold) |
| `spendgate_ledger_pool_wait_ms` (histogram) | none | each Postgres transaction's wait for a pool connection |
| `spendgate_ledger_pool_size`, `_in_use`, `_max` (gauges) | none | the Postgres pool, sampled at each scrape |
| `spendgate_sweeper_expired_total` | none | expired holds the sweeper charged in full |
| `spendgate_unsettled_closes` (gauge) | none | the closer's closes the ledger has not taken, sampled at each scrape |
| `spendgate_heartbeat_failures_total` | `step`: `renew_leases`, `watch_streams`, `sweep` | a heartbeat step that raised |
| `spendgate_gateway_overhead_ms` (histogram) | none | a held call's latency minus its summed upstream attempt latencies |

Label values are fixed sets in the code; an unknown `op` or `step` is folded into `other`, so a
future caller cannot mint series. The pipeline reports the new facts through two more `Observer`
methods (`on_ledger`, `on_heartbeat_failed`) and a new `SettledEvent.upstream_ms`; as before, an
observer that raises changes nothing. The Postgres ledger takes a `pool_wait` hook and offers
`pool_stats()`; SQLite has no pool and publishes no pool series.

**Tracing** (a new optional extra, `otel`: `opentelemetry-api`, `opentelemetry-sdk` and
`opentelemetry-exporter-otlp-proto-http`):

- Config: `observability.otel: {enabled: false, service_name: "spendgate"}`. Tracing is on only
  when the extra is installed and `enabled` is true. Enabled without the extra, `serve` prints a
  warning at start and serves untraced; the money path does not depend on tracing, so refusing to
  start would only cost availability.
- The exporter is OTLP over HTTP, configured only by the standard `OTEL_EXPORTER_OTLP_*`
  variables (endpoint, headers, timeout, certificate); `OTEL_TRACES_SAMPLER` picks the sampler.
  The tracer provider is local to the gateway: nothing global is set.
- One span per hold (`spendgate.hold`), forward (`spendgate.forward`, kind client, around every
  attempt of the call) and settle (`spendgate.settle`). The pipeline sees only a `Tracer` port
  (`pipeline/tracing.py`, `NoTracer` by default); `gateway/otel.py` is the one module that imports
  OpenTelemetry, which an import-linter contract enforces.
- `traceparent` is read from the request and is the parent of the three spans; without a valid
  one, the hold span starts a new trace and the other two are its children. The provider request
  gets a `traceparent` naming the forward span. `tracestate` and baggage are caller-made values
  and are neither read nor passed on.
- Span attributes are an allowlist, as for logs: `spendgate.request_id`, `spendgate.model` (the
  catalog id), `spendgate.endpoint`, `spendgate.deployment` (the config label), `spendgate.outcome`,
  `spendgate.attempts`, `spendgate.released`, `spendgate.booked` and `error.type`. Values must be
  scalar, short and not shaped like a key. Exceptions are not recorded as span events and the
  status carries no description, because an exception message can quote a body or a secret.
- The pipeline wraps a plugged-in tracer once (`tracing.Guarded`): whatever the tracer raises is
  dropped and it can never swallow a step's own error, so a tracing fault cannot answer 500 for a
  held call or leave its hold to be charged in full by the sweeper.

## Consequences

- No new runtime dependency: the base install still has five. The extra pulls in protobuf,
  `googleapis-common-protos` and `requests` (already in the `google` extra), and is not in the
  `full` image; an operator who wants traces installs `spendgate[otel]` or builds an image with it.
- This is not telemetry in the sense of decision D5: nothing is sent unless the operator installs
  the extra, enables it and points the exporter at a collector they run. The default endpoint is
  the OTLP default (`localhost:4318`), never a host of the project.
- Spans carry the request id, which also appears in the call log and the `calls` table, so a trace
  can be matched to its ledger row. The provider sees a trace id and a span id it did not see
  before; both are random and carry nothing about the caller.
- Library mode builds no tracer and binds no metrics; it keeps logging as before.
- The hold step moved from `pipeline/pipeline.py` to `pipeline/holding.py` (`Holder`), so the
  module stays under the 400-line guideline with the spans added; behaviour is unchanged.
- Documented in [operations.md](../operations.md#metrics-and-logs) (metrics, alert rules,
  tracing) and the [configuration reference](../configuration.md#otel).
