# Roadmap

What may come after v0.1. Nothing here is a promise or a date. Items move when someone needs them;
open a [feature request](.github/ISSUE_TEMPLATE/feature_request.yml) to argue for one. Anything that
would change public behaviour gets an ADR first (see [CONTRIBUTING.md](CONTRIBUTING.md)).

## Providers and pricing

- A generic OpenAI-compatible provider (vLLM, Ollama, OpenRouter and similar), so a new
  OpenAI-shaped endpoint needs catalog entries and configuration rather than a new adapter.
- More dedicated providers where the OpenAI shape does not fit, such as Mistral.
- OpenAI Realtime speech adapter, with its catalog entries.
- Batch APIs and image-generation pricing.
- Cross-model fallback (hold on the union of budgets at the dearest price). v0.1 fails over only
  between deployments of the same catalog id.
- Image ceilings per `detail` level (low, high, original), so a low-detail image is not held at
  the highest-detail cost.
- Opt-in, signed price-catalog updates, so prices stay current without a release. Never on by default.

## Budgets, limits and reporting

- Per-instance budget slices: each instance holds against a slice of a budget it leased in
  advance, so it keeps a bounded spend through a database failover and no single budget row is
  hot under load. Changes the ledger schema and the refusal rules, so it needs an ADR.
- Per-end-user budgets keyed on `X-Spendgate-User`, so a product can cap each of its customers
  without minting a key per customer. Today the header is a label for reports only.
- Weekly and rolling budget windows (for example the last 7 days), for spend that is planned per
  week or must not burst at a period boundary. v0.1 has lifetime, daily and monthly budgets.
- A public hold and settle API, so another gateway (for example a LiteLLM hook) can use the ledger
  for its holds and settlements without sending calls through the proxy.
- A shared limiter store (for example Redis) so RPM and TPM limits are exact across several
  instances. Limits are per process in v0.1; budgets are already exact in the ledger.
- A bounded wait queue per model group, to smooth provider throttling instead of failing fast.
- Email for soft-threshold alerts, beside the webhook.
- Reconciliation against provider billing and usage APIs (a drift report).
- Daily usage rollups, so long-term reporting survives the `calls` retention window.
- A read-only usage page served by the proxy, so people can see their spend without admin access
  or a separate dashboard.
- A Grafana dashboard.

## Library mode

- Library mode on Python 3.12, so services that cannot move to 3.13 yet can use it. The package
  needs 3.13 or newer today.

## Deployment and security

- Documented deployment patterns beyond Docker Compose (single VM with HTTPS, managed containers on
  the large clouds, Kubernetes with NetworkPolicy as the perimeter). A pattern is published only
  after it has been deployed and its verification steps pass.
- An optional egress host allowlist, so the proxy can reach only the configured provider hosts.
- Admin SSO (OIDC) and mapping company tokens to keys.
- Infrastructure-as-code modules, if there is demand.

## Performance

- Under many concurrent calls on one budget chain, the PostgreSQL ledger is still limited by waits on
  the hot budget row, even after 0.2.0 cut a call to 2 transactions and 5 round trips (see the
  [load review](docs/reviews/load-v0.1.md)). Per-instance budget slices (above) are one way to remove
  that row from the hot path.

## Done

- Soft-threshold webhook alerts before a cap is reached (0.2.0).
- OpenTelemetry tracing as an extra (0.2.0).
- Fewer PostgreSQL round trips per call: 5 instead of about 46 (0.2.0).

## Known limitations in v0.1

- Limits (RPM, TPM, concurrency) are per process.
- Usage covers the retention window only (default 90 days).
- Library (SDK-only) mode is cooperative: code holding a provider key can skip it. The proxy with
  the Compose perimeter is the enforced mode.
- No Realtime endpoint.

## Not planned

Response caching, prompt guardrails and PII scanning, fine-tuning, MCP, and multi-tenant SaaS
hosting. Revisiting any of these needs an ADR.
