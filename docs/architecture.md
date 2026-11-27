# spendgate architecture

Status: current for v0.1.1 (first written 2026-11-13). Decisions: [decisions.md](./decisions.md). ADRs: [0001](adr/0001-ports-and-adapters.md) to [0022](adr/0022-stable-error-codes.md) (0020 was not used).

## 1. Components

```mermaid
flowchart LR
  subgraph callers
    A1[App using OpenAI/Anthropic SDK]
    A2[App using spendgate.client]
    A3[Python code in-process]
    OP[Operator]
  end
  A1 -->|HTTP| GW
  A2 -->|HTTP| GW
  OP -->|spendgate CLI| CLI
  CLI -->|admin API| GW
  CLI -->|ledger functions| LED
  A3 --> LOC
  GW[spendgate.gateway: Starlette app, admin, /livez /readyz /metrics] --> PIPE
  LOC[spendgate.local: guard, LocalPrincipal, sync and async transports] --> PIPE
  PIPE[spendgate.pipeline: steps, routing, streaming relay, key verification]
  PIPE --> CORE[spendgate.core: money, catalog, cost, estimate, policy, limits, health (cooldown + circuit breaker), outcomes]
  PIPE --> LED[(spendgate.ledger: SQLite or Postgres, one shared sql.py)]
  PIPE --> PROV[spendgate.providers: one adapter each]
  PROV -->|HTTPS| UP[Provider APIs]
  CFG[spendgate.config: reads YAML + sha256] --> PIPE
  CFG -->|dicts, sha| CORE
```

## 2. Package map

Every component above is exactly one package. Rows show the one-line job and the allowed internal imports.

| Package | Responsibility | May import |
|---|---|---|
| `spendgate.core` | Pure rules: money, catalog (`catalog.parse(dicts, sha)`), cost, estimate, audio, media (which inline media is priceable), policy, limits, `DeploymentHealth` (one tracker merging cooldown and circuit breaker), outcomes (including `not_processed`), errors, `litellm` (LiteLLM's price file mapped to draft catalog entries, for `spendgate catalog import` only) and `standin` (an entry's meters times a multiplier of at least 1, for `spendgate catalog draft`). No I/O, never reads files. | nothing internal |
| `spendgate.ledger` | Two ports: `port.MoneyLedger` (hold, settle, release, renew, sweep, totals, scope, kill switches: all the pipeline may call) and `admin_port.AdminLedger`, which extends it with the admin writes, keys and reads. SQLite and Postgres adapters over one shared `sql.py` implement both; keys, usage queries, SQL migrations (one pure `pending_migrations` decides what runs for both backends). | `core` |
| `spendgate.providers` | Provider port plus one adapter per provider; slug = module name: `openai`, `azure_openai`, `anthropic`, `bedrock`, `google`, `azure_speech`, `elevenlabs`. `openai` and `azure_openai` share the OpenAI wire half (`openai_wire`) and differ only in URL and auth. | `core` |
| `spendgate.config` | Reads `spendgate.yaml` and catalog YAML files, computes their sha256, produces typed settings and the dicts for `core.catalog.parse`. | `core` |
| `spendgate.client` | SDK helpers and proxy-mode client: headers, error mapping. | `core` |
| `spendgate.pipeline` | The 10 steps, routing (ordered deployments plus `DeploymentHealth`), streaming relay, key verification. No HTTP framework. Also the composition helper `pipeline/adapters.py` (`build_providers`): the one module that imports the concrete provider adapters, because it builds them from `Settings` and `providers` and `config` may not meet. It is the only place that does, and the gateway and library mode both call it. `pipeline/assembly.py` builds the parts a `Pipeline` owns from its keyword arguments (`Pipeline.__init__` declares them and their defaults), so `pipeline.py` holds only the steps. | `core`, `ledger.port`, `providers.port`, `config`; `providers.*` from `adapters.py` only, `ledger.ids` from `assembly.py` only (request and instance ids), `ledger.key_types` for types |
| `spendgate.gateway` | Starlette app, admin API, observability, the optional budget-warning webhook (`alert_webhook.py`, a handler on the `spendgate.budget` logger with its own background poster). Wires ledger, providers and config in one composition root, `gateway/compose.py` (`build_gateway`: data app, admin app, `Maintenance`), which `serve` and the integration test harness both call. `gateway/otel.py` is the only module that imports OpenTelemetry (the optional `otel` extra), behind the pipeline's `Tracer` port. | `pipeline` and below |
| `spendgate.local` | In-process library mode: `spendgate.local.guard(cap="5 USD", catalog=None)` builds the same pipeline with a `LocalPrincipal`; the ledger currency follows the cap's currency (bundled catalog is USD; another currency needs `fx=` and otherwise fails with a clear message); sync transport runs its own loop thread (safe in notebooks) plus an async transport; the `httpx` and `httpx2` pairs share the runtime builder (`local/factory.py`) and the stream halves (`local/bridge.py`). Never imports `gateway`. | `pipeline` and below |
| `spendgate.cli` | Command line: serve, keys, budgets, usage, kill switch, migrate. Thin layer over the same ledger functions as the admin API. Also `catalog import` (`cli/catalog.py`), the only code that downloads anything, by operator action and never while serving ([ADR-0014](adr/0014-catalog-import-command.md)), and `catalog draft`, which writes a stand-in price for a model the catalog lacks, optionally for some Regions only ([ADR-0028](adr/0028-price-coverage-and-regional-prices.md)). | everything |

Notes:
- `catalog/prices.yaml` and `catalog/schema.json` are data, not a package. `config` reads them; `core.catalog.parse` validates and resolves.
- Only `spendgate.gateway` imports Starlette: today `gateway.app`, `gateway.admin`, `gateway.admin_http`, `gateway.admin_budgets`, `gateway.admin_scopes`, `gateway.admin_reports` and `gateway.observability`. `cli.serve` imports uvicorn to run the gateway apps, and nothing else imports uvicorn. `pipeline` stays framework free so `local` can reuse it. Three import-linter contracts check this ("pipeline has no HTTP framework", "only the gateway imports Starlette", "only cli.serve imports uvicorn"). A fourth, "only the catalog command uses the LiteLLM mapper", keeps `core.litellm` and `cli.catalog` out of reach of everything that serves calls.
- No package is named `utils` or `common`. Shared code goes to `core` if pure, or to its owner package.
- Sibling adapters never import each other: `ledger.sqlite` vs `ledger.postgres`, and each provider module vs the others.
- Limits (RPM, TPM, concurrency) are per process in v0.1; budgets are exact in the ledger. A shared limiter store is on the [roadmap](../ROADMAP.md).

## 3. Request path

Pipeline order (fixed, one function per step in `spendgate.pipeline`):

1. Authenticate from the headers (`Authorization: Bearer sg-<prefix>-<secret>`), before any body byte is read, so a caller without a valid key never holds a gate place or memory (SR-01).
2. Body size and concurrency: the declared `Content-Length`, the key's in-flight uploads, then the size and concurrency gate; only then is the body read under its cap and parsed (`pipeline/intake.py`).
3. Endpoint allowlist validation (protocol section 2); no provider translation yet.
4. Route: candidate deployments of the same catalog id, skipping cooled ones (`model_not_priced` if unpriced).
5. Price, plan budgets, worst case = max over candidates, output ceiling (protocol 2.1).
6. Admit limits (RPM, TPM, concurrency); refund on any later refusal.
7. Hold: one admission transaction that checks kill switches, session limits and budgets (ledger-state-machine.md). Ledger down, or slower than the hold deadline ([operations.md](operations.md#postgres-connections)), means `ledger_unavailable`. Kill-switch checks outside the transaction are only a cache.
8. Adapter `prepare()` (credential refresh in a worker thread, off the event loop), `translate()` and forward; only when `not_processed(attempt)` holds (never sent, or a `free_statuses` answer), next candidate within the same reservation (no second hold), at cost 0. A 5xx or timeout after send is returned without failover.
9. Settle once on the reservation (after send, a cancel or failure is the full hold unless usage was seen, for a stream its final usage, M-13), or `release` if every attempt was `not_processed`.
10. Record (`calls` row per attempt) and emit headers, logs and metrics.

Both entry points, the gateway and library mode, call `Pipeline.serve`, which runs steps 1 and 2 and then steps 3 to 10 on the parsed request. Library mode sends a placeholder key that its local key store accepts.

Refused requests write no `calls` row (logs and metrics only). Steps 3 to 6 are functions in `pipeline/steps.py` (`prepare`, `admit_limits`) that call the pure rules in `core` (catalog, estimate, cost, policy, limits); they contact neither a provider nor the ledger. Steps 7 and 9 use the money port (`ledger.port.MoneyLedger`). Step 8 uses the provider port.

## 4. Dependency rules

1. `core` imports nothing from `spendgate`, and no I/O library (no httpx, Starlette, sqlite3, psycopg, yaml).
2. `ledger`, `providers`, `config`, `client` import `core` only, and never each other (`pipeline` imports their ports).
3. Siblings inside `ledger` and inside `providers` do not import each other (only `port`).
4. `pipeline` is the single wiring layer below the entry points; it imports no Starlette/uvicorn.
5. `gateway` and `local` are siblings and never import each other. `cli` sits on top and nothing imports it.

### import-linter configuration

```toml
[tool.importlinter]
root_package = "spendgate"
include_external_packages = true

[[tool.importlinter.contracts]]
name = "core imports nothing internal and no I/O libraries"
type = "forbidden"
source_modules = ["spendgate.core"]
forbidden_modules = [
  "spendgate.ledger", "spendgate.providers", "spendgate.pipeline", "spendgate.gateway",
  "spendgate.client", "spendgate.local", "spendgate.config", "spendgate.cli",
  "httpx", "starlette", "uvicorn", "yaml", "psycopg", "sqlite3",
]

[[tool.importlinter.contracts]]
name = "leaf packages are independent of each other"
type = "independence"
modules = [
  "spendgate.ledger", "spendgate.providers", "spendgate.config", "spendgate.client",
]

[[tool.importlinter.contracts]]
name = "ledger backends are independent"
type = "independence"
modules = ["spendgate.ledger.sqlite", "spendgate.ledger.postgres"]

[[tool.importlinter.contracts]]
name = "provider adapters are independent"
type = "independence"
modules = [
  "spendgate.providers.openai", "spendgate.providers.azure_openai",
  "spendgate.providers.anthropic", "spendgate.providers.bedrock",
  "spendgate.providers.google", "spendgate.providers.azure_speech",
  "spendgate.providers.elevenlabs",
]

[[tool.importlinter.contracts]]
name = "layers: cli > gateway | local > pipeline > leaf packages > core"
type = "layers"
layers = [
  "spendgate.cli",
  "spendgate.gateway | spendgate.local",
  "spendgate.pipeline",
  "spendgate.ledger | spendgate.providers | spendgate.config | spendgate.client",
  "spendgate.core",
]

[[tool.importlinter.contracts]]
name = "pipeline has no HTTP framework"
type = "forbidden"
source_modules = ["spendgate.pipeline"]
forbidden_modules = ["starlette", "uvicorn"]

[[tool.importlinter.contracts]]
name = "only the gateway imports Starlette"
type = "forbidden"
source_modules = ["spendgate"]
forbidden_modules = ["starlette"]
ignore_imports = ["spendgate.gateway.** -> starlette"]
allow_indirect_imports = true

[[tool.importlinter.contracts]]
name = "only the catalog command uses the LiteLLM mapper"
type = "forbidden"
source_modules = [
  "spendgate.gateway", "spendgate.local", "spendgate.pipeline", "spendgate.ledger",
  "spendgate.providers", "spendgate.config", "spendgate.client", "spendgate.cli.serve",
]
forbidden_modules = ["spendgate.core.litellm", "spendgate.cli.catalog"]

[[tool.importlinter.contracts]]
name = "only cli.serve imports uvicorn"
type = "forbidden"
source_modules = ["spendgate"]
forbidden_modules = ["uvicorn"]
ignore_imports = ["spendgate.cli.serve -> uvicorn"]
allow_indirect_imports = true

[[tool.importlinter.contracts]]
name = "only gateway.otel imports OpenTelemetry"
type = "forbidden"
source_modules = ["spendgate"]
forbidden_modules = ["opentelemetry"]
ignore_imports = ["spendgate.gateway.otel -> opentelemetry"]
allow_indirect_imports = true
```

Notes: `include_external_packages` is needed for the external bans. The Starlette and uvicorn contracts are allow-lists: the whole package is the source, so a new package is covered at once, and only the `ignore_imports` entries are exempt. They check direct imports only, because `cli` imports `gateway` to serve it; the `layers` contract already keeps every other package away from `gateway`. The `|` layer is independent (siblings), so `local` cannot import `gateway` and vice versa; the `layers` contract also stops lower layers importing upward. The `independence` contract on `spendgate.providers.*` must list real modules, so add each file when it lands. Provider SDKs (boto3, google-auth) may be imported only inside their own adapter, as optional extras. One exception: `ledger.postgres_auth` imports botocore, azure-identity and google-auth (lazily, from the `bedrock`, `azure` and `google` extras) to get database login tokens for `ledger.backend.auth` ([ADR-0017](adr/0017-cloud-identity.md)).

## 5. Dependencies (base install: at most 6, 5 used)

Starlette, uvicorn, httpx, Pydantic, PyYAML. One slot stays free. Postgres (`psycopg`), boto3, Google auth and OpenTelemetry (`otel`) are extras. SQLite is in the standard library.

## 6. Decisions

- [ADR-0001](adr/0001-ports-and-adapters.md): ports and adapters.
- [ADR-0002](adr/0002-starlette-over-fastapi.md): Starlette over FastAPI.
- [ADR-0003](adr/0003-fail-closed.md): fail closed.
- [ADR-0004](adr/0004-integer-money.md): integer money.
- [ADR-0005](adr/0005-sqlite-and-postgres-no-redis.md): SQLite and Postgres, no Redis in v0.1.
- [ADR-0006](adr/0006-raise-product-size-limit.md): raise the product size limit (superseded by ADR-0007).
- [ADR-0007](adr/0007-drop-the-total-size-limit.md): drop the total product-size limit.
- [ADR-0008](adr/0008-scope-display-name.md): scope display name (migration 0003).
- [ADR-0009](adr/0009-review-hardening.md): behaviour changes from the v0.1 code and security reviews.
- [ADR-0010](adr/0010-calls-http-status.md): `calls.http_status` is the provider's status, range-checked (migration 0004).
- [ADR-0011](adr/0011-model-filter-validation-and-late-settle-reservation.md): refuse a `model_filter` that matches no route; a late settle updates the reservation.
- [ADR-0012](adr/0012-bounded-ledger-waits.md): bounded ledger waits (Postgres session limits, hold deadline).
- [ADR-0013](adr/0013-safer-agent-defaults.md): safer defaults for agents: `session_required`, a new-session bound and a concurrency limit.
- [ADR-0014](adr/0014-catalog-import-command.md): `spendgate catalog import` drafts an override file from LiteLLM's price file.
- [ADR-0015](adr/0015-readiness-and-shutdown.md): a kill switch leaves readiness alone; keep-alive and drain deadline settings.
- [ADR-0016](adr/0016-alert-webhook.md): budget warnings posted to a signed webhook.
- [ADR-0017](adr/0017-cloud-identity.md): cloud identity, secret reload on SIGHUP, ledger pool settings.
- [ADR-0018](adr/0018-migration-policy.md): migrations are a separate step (`serve` migrates Postgres only when asked); code accepts one compatible step ahead.
- [ADR-0019](adr/0019-otel-extra.md): ledger health metrics and the optional OpenTelemetry extra (`otel`).
- [ADR-0021](adr/0021-short-throttle-cooldown.md): short jittered throttle cooldown and a bounded wait for a cooling deployment.
- [ADR-0022](adr/0022-stable-error-codes.md): `calls.error_code` holds stable, documented codes.
- [ADR-0026](adr/0026-elevenlabs-language-code.md): ElevenLabs speech accepts a validated ISO 639-1 `language_code`.
- [ADR-0027](adr/0027-admin-network.md): the admin API on its own Compose network, which only the operator service joins.
- [ADR-0028](adr/0028-price-coverage-and-regional-prices.md): price coverage, Region-scoped catalog entries (`regions`), one price per route, and how the catalog stays current.
- [ADR-0029](adr/0029-audio-duration-is-counted.md): audio duration is counted from the packets a decoder decodes, never read from the container header; a size floor backs the count.
