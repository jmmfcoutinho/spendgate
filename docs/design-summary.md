# spendgate design at a glance

**spendgate** is a small, auditable LLM spend guard: a Python SDK plus an OpenAI/Anthropic-compatible proxy. The proxy holds the provider keys and refuses any call that could break a budget, before the provider is contacted. v0.1 is built and tested locally; cloud patterns come later (see the [roadmap](../ROADMAP.md)).

## What it guarantees

1. **Hard budgets.** Before forwarding, the worst-case cost is held in every budget the call belongs to (org → project → service → user → session), in one database transaction. Afterwards the real cost is settled once. Invariant: `spent + held ≤ effective cap + recorded overrun + recorded excess`. Overruns (real cost above the hold) are recorded and alerted. By default they block the budget until acknowledged. A call is free only if it never reached the provider, the provider throttled it (429), or the provider rejected it with an error and no usage. Anything else after sending (5xx, timeout, stream cut, client cancel) is charged the full hold unless usage came back, so the provider can never bill something we didn't book.
2. **Fail closed.** The guard refuses before any upstream contact on any of these:
   - unknown or unpriced model
   - missing FX rate or output ceiling
   - unreachable ledger
   - kill switch
   - bad key

   Price data cannot switch this off. Any opt-out lives in `spendgate.yaml` under `fail_open:` and is logged at start.
3. **Agent sessions are bounded.** Each session id gets a budget, a step limit, a time limit and an idle expiry, taken from the key's defaults. A runaway loop gets `session_limit_reached`.
4. **Provider throttling handled.** A provider 429 cools that deployment down (honouring `Retry-After`). The guard then tries the next deployment of the same model within the same reservation. Failover happens only when the provider did not process the attempt, so it is free and never double-charged. If none is left, it returns `upstream_throttled`, never `budget_exhausted`.
5. **Traceable.** Every call records key, scopes, session, user, tags, model, deployment, tokens, price row, catalog checksum, cost, FX and latency. `spendgate usage --by service,model --since 7d` answers who spent what.
6. **Unbypassable when deployed with the perimeter.** In Docker Compose, apps sit on an internal network and only the guard has provider keys and internet access. Automated tests prove it.

## How it is built

- **Pure core:** money as integer nano-units, prices, cost, estimates, policy, limits and settlement rules.
- **One I/O-free `pipeline` package** with 10 ordered steps. It is driven by the HTTP `gateway` (proxy mode) or by an httpx transport (`local`, SDK mode). The SDK quick start is one call, `spendgate.local.guard(cap="5 USD")`; a cap in another currency needs an FX rate.
- **Providers:** one small adapter per provider (OpenAI, Azure OpenAI, Anthropic, Bedrock, Google, Azure Speech, ElevenLabs).
- **Ledger:** SQLite for one host, Postgres for shared use, behind one contract tested identically on both.
- **Prices are data:** `catalog/prices.yaml`, with a JSON Schema. A new model is a data edit.
- **Small and auditable** (checked in CI): modules ≤ 400 lines and functions ≤ 50 (the total-size limit was dropped by [ADR-0007](adr/0007-drop-the-total-size-limit.md)), ≤ 6 base dependencies, no ORM, no Redis, no telemetry, and a one-page `how-money-is-protected.md` naming the files and tests behind each guarantee.

## Design documents

| Doc | What it fixes |
|---|---|
| [decisions.md](./decisions.md) | Name, Apache-2.0 licence, hosting, EUR, no telemetry, providers, Python 3.13+, local-first |
| [architecture.md](./architecture.md) + [ADRs 0001–0005](./adr/) | Packages, import rules, pipeline order, Starlette, fail closed, integer money, SQLite + Postgres |
| [protocol.md](./protocol.md) + [admin-openapi.yaml](./admin-openapi.yaml) | Endpoints, allowlists, headers, error codes and statuses, admin API |
| [data-model.md](./data-model.md) + [ledger-state-machine.md](./ledger-state-machine.md) | Tables (SQL), reservation states, 19 money scenarios (simulated in SQLite) |
| [catalog-schema.md](./catalog-schema.md) (+ `.json`, example) | Price file format; every price of the earlier in-house guard expressed as data |
| [threat-model.md](./threat-model.md) | 19 threats, controls, test ids, 5 accepted risks |
| [CONTRIBUTING.md](../CONTRIBUTING.md) + `lightweight_report.py` | Code style, tests, quality gates, size report, release process |
| [reviews/](reviews/) | Code, security, load, verification and live-smoke reviews of v0.1, with findings and severities |

## Accepted risks in v0.1

| Risk | Why accepted |
|---|---|
| The guard itself may reach any internet host (no egress allowlist yet) | Network perimeter still blocks apps; allowlist is on the roadmap |
| Whoever controls the host (root) can read keys and edit the ledger | Out of scope for an application; protect the host |
| A key holder can mislabel user, tags or session inside its own scope | Labels never move money to another budget |
| Provider-reported usage is billed as reported | Absurd numbers are flagged and alerted, never silently capped |
| A key holder can lock its own budget with open holds until leases expire | Limits per key bound it; leases expire |

Also by design: SDK-only (library) mode is cooperative. Code holding a provider key can skip it; the proxy plus perimeter is the enforced mode.

## Still open

Some catalog prices are still `verified: false` until re-checked against the provider's page. The trademark registers (EUIPO, USPTO, UK IPO) have not been searched yet; see [decisions.md](decisions.md).
