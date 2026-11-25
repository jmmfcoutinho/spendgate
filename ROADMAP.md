# Roadmap

What may come after v0.1. Nothing here is a promise or a date. Items move when someone needs them;
open a [feature request](.github/ISSUE_TEMPLATE/feature_request.yml) to argue for one. Anything that
would change public behaviour gets an ADR first (see [CONTRIBUTING.md](CONTRIBUTING.md)).

## Providers and pricing

- More providers: Mistral, OpenRouter, local Ollama and vLLM (zero-cost meters).
- OpenAI Realtime speech adapter, with its catalog entries.
- Batch APIs and image-generation pricing.
- Cross-model fallback (hold on the union of budgets at the dearest price). v0.1 fails over only
  between deployments of the same catalog id.
- Image ceilings per `detail` level (low, high, original), so a low-detail image is not held at
  the highest-detail cost.
- Opt-in, signed price-catalog updates, so prices stay current without a release. Never on by default.

## Budgets, limits and reporting

- A shared limiter store (for example Redis) so RPM and TPM limits are exact across several
  instances. Limits are per process in v0.1; budgets are already exact in the ledger.
- A bounded wait queue per model group, to smooth provider throttling instead of failing fast.
- Soft-threshold alerts before a cap is reached: **done** as `spendgate.budget` log warnings and
  an optional signed webhook (`alerts.webhook`, see
  [operations](docs/operations.md#budget-warning-webhook)). Email alerts are still open.
- Reconciliation against provider billing and usage APIs (a drift report).
- Daily usage rollups, so long-term reporting survives the `calls` retention window.
- A read-only web dashboard for usage.
- A Grafana dashboard over the Prometheus metrics (OpenTelemetry tracing is the `otel` extra).

## Deployment and security

- Documented deployment patterns beyond Docker Compose (single VM with HTTPS, managed containers on
  the large clouds, Kubernetes with NetworkPolicy as the perimeter). A pattern is published only
  after it has been deployed and its verification steps pass.
- An optional egress host allowlist, so the proxy can reach only the configured provider hosts.
- Admin SSO (OIDC) and mapping company tokens to keys.
- Infrastructure-as-code modules, if there is demand.

## Performance

- The PostgreSQL ledger adds more latency per call than SQLite and degrades with concurrency (see the
  [load review](docs/reviews/load-v0.1.md)). Reducing that overhead is the main performance goal.

## Known limitations in v0.1

- Limits (RPM, TPM, concurrency) are per process.
- Usage covers the retention window only (default 90 days).
- Library (SDK-only) mode is cooperative: code holding a provider key can skip it. The proxy with
  the Compose perimeter is the enforced mode.
- No Realtime endpoint.

## Not planned

Response caching, prompt guardrails and PII scanning, fine-tuning, MCP, and multi-tenant SaaS
hosting. Revisiting any of these needs an ADR.
