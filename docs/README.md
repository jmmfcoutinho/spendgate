# Documentation

Start with the [project README](../README.md) for the five-minute quickstart.

## Using spendgate

| Doc | What it covers |
|---|---|
| [how-money-is-protected.md](how-money-is-protected.md) | Each guarantee, the code and tests behind it, and the money rules M-01 to M-13 |
| [deploy/local.md](deploy/local.md) | Running locally: SDK only, local proxy, Docker Compose with a perimeter |
| [library-mode.md](library-mode.md) | The SDK without a server, and its limits |
| [configuration.md](configuration.md) | Every `spendgate.yaml` key (generated from the code) and [examples/](../examples/) |
| [operations.md](operations.md) | Backups, keys, kill switch, usage queries, retention, sweeper, metrics, logs |
| [catalog-how-to.md](catalog-how-to.md) | Add a model or a price |
| [provider-how-to.md](provider-how-to.md) | Add a provider adapter |
| [litellm-comparison.md](litellm-comparison.md) | When to use spendgate and when LiteLLM |

## Reference and design

| Doc | What it covers |
|---|---|
| [architecture.md](architecture.md) and [adr/](adr/) | Packages, import rules, request pipeline, architecture decisions |
| [protocol.md](protocol.md) and [admin-openapi.yaml](admin-openapi.yaml) | Endpoints, headers, error codes, admin API |
| [data-model.md](data-model.md) and [ledger-state-machine.md](ledger-state-machine.md) | Tables and reservation states, with the money scenarios |
| [catalog-schema.md](catalog-schema.md) and [catalog-example.yaml](catalog-example.yaml) | Price catalog format; the schema is `src/spendgate/catalog/schema.json` |
| [threat-model.md](threat-model.md) | Threats, controls, test ids, accepted risks |

What may come next is in the [roadmap](../ROADMAP.md). Contributor rules are in [CONTRIBUTING.md](../CONTRIBUTING.md); how to report problems and ideas in
[issues-and-prs.md](issues-and-prs.md); security reports in [SECURITY.md](../SECURITY.md).
