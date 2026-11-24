# spendgate architecture

Status: proposed, 2026-11-13. Decisions: [decisions.md](./decisions.md). ADRs: [0001](adr/0001-ports-and-adapters.md) to [0005](adr/0005-sqlite-and-postgres-no-redis.md).

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
| `spendgate.core` | Pure rules: money, catalog (`catalog.parse(dicts, sha)`), cost, estimate, audio, media (which inline media is priceable), policy, limits, `DeploymentHealth` (one tracker merging cooldown and circuit breaker), outcomes (including `not_processed`), errors. No I/O, never reads files. | nothing internal |
| `spendgate.ledger` | Ledger port, SQLite and Postgres adapters over one shared `sql.py`, keys, usage queries, SQL migrations. | `core` |
| `spendgate.providers` | Provider port plus one adapter per provider; slug = module name: `openai`, `azure_openai`, `anthropic`, `bedrock`, `google`, `azure_speech`, `elevenlabs`. | `core` |
| `spendgate.config` | Reads `spendgate.yaml` and catalog YAML files, computes their sha256, produces typed settings and the dicts for `core.catalog.parse`. | `core` |
| `spendgate.client` | SDK helpers and proxy-mode client: headers, error mapping. | `core` |
| `spendgate.pipeline` | The 10 steps, routing (ordered deployments plus `DeploymentHealth`), streaming relay, key verification. No HTTP framework. Also the composition helper `pipeline/adapters.py` (`build_providers`): the one module that imports the concrete provider adapters, because it builds them from `Settings` and `providers` and `config` may not meet. It is the only place that does, and the gateway and library mode both call it. | `core`, `ledger.port`, `providers.port`, `config`; `providers.*` and `ledger.ids`/`ledger.key_types` from `adapters.py` and the type imports of `pipeline.py` only |
| `spendgate.gateway` | Starlette app, admin API, observability. Wires ledger, providers and config. | `pipeline` and below |
| `spendgate.local` | In-process library mode: `spendgate.local.guard(cap="5 USD", catalog=None)` builds the same pipeline with a `LocalPrincipal`; the ledger currency follows the cap's currency (bundled catalog is USD; another currency needs `fx=` and otherwise fails with a clear message); sync transport runs its own loop thread (safe in notebooks) plus an async transport. Never imports `gateway`. | `pipeline` and below |
| `spendgate.cli` | Command line: serve, keys, budgets, usage, kill switch, migrate. Thin layer over the same ledger functions as the admin API. | everything |

Notes:
- `catalog/prices.yaml` and `catalog/schema.json` are data, not a package. `config` reads them; `core.catalog.parse` validates and resolves.
- Only `gateway.app`, `gateway.admin` and `gateway.observability` may import Starlette. `pipeline` stays framework free so `local` can reuse it. A contract checks this.
- No package is named `utils` or `common`. Shared code goes to `core` if pure, or to its owner package.
- Sibling adapters never import each other: `ledger.sqlite` vs `ledger.postgres`, and each provider module vs the others.
- Limits (RPM, TPM, concurrency) are per process in v0.1; budgets are exact in the ledger. A shared limiter store is on the [roadmap](../ROADMAP.md).

## 3. Request path

Pipeline order (fixed, one function per step in `spendgate.pipeline`):

1. Body size and concurrency check, before reading the body.
2. Authenticate (`Authorization: Bearer sg-<prefix>-<secret>`).
3. Endpoint allowlist validation (protocol section 2); no provider translation yet.
4. Route: candidate deployments of the same catalog id, skipping cooled ones (`model_not_priced` if unpriced).
5. Price, plan budgets, worst case = max over candidates, output ceiling (protocol 2.1).
6. Admit limits (RPM, TPM, concurrency); refund on any later refusal.
7. Hold: one admission transaction that checks kill switches, session limits and budgets (ledger-state-machine.md). Ledger down means `ledger_unavailable`. Kill-switch checks outside the transaction are only a cache.
8. Adapter `translate()` and forward; only when `not_processed(attempt)` holds (never sent, or a `free_statuses` answer), next candidate within the same reservation (no second hold), at cost 0. A 5xx or timeout after send is returned without failover.
9. Settle once on the reservation (after send, a cancel or failure is the full hold unless usage was seen, for a stream its final usage, M-13), or `release` if every attempt was `not_processed`.
10. Record (`calls` row per attempt) and emit headers, logs and metrics.

Refused requests write no `calls` row (logs and metrics only). Steps 4 to 6 run in `core` (pure). Steps 7 and 9 use the ledger port. Step 8 uses the provider port.

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
```

Notes: `include_external_packages` is needed for the external bans. The `|` layer is independent (siblings), so `local` cannot import `gateway` and vice versa; the `layers` contract also stops lower layers importing upward. The `independence` contract on `spendgate.providers.*` must list real modules, so add each file when it lands. Provider SDKs (boto3, google-auth) may be imported only inside their own adapter, as optional extras.

## 5. Dependencies (base install: at most 6, 5 used)

Starlette, uvicorn, httpx, Pydantic, PyYAML. One slot stays free. Postgres (`psycopg`), boto3 and Google auth are extras. SQLite is in the standard library.

## 6. Decisions

- [ADR-0001](adr/0001-ports-and-adapters.md): ports and adapters.
- [ADR-0002](adr/0002-starlette-over-fastapi.md): Starlette over FastAPI.
- [ADR-0003](adr/0003-fail-closed.md): fail closed.
- [ADR-0004](adr/0004-integer-money.md): integer money.
- [ADR-0005](adr/0005-sqlite-and-postgres-no-redis.md): SQLite and Postgres, no Redis in v0.1.
