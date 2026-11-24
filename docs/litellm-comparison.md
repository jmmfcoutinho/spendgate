# spendgate and LiteLLM

[LiteLLM](https://github.com/BerriAI/litellm) is a widely used gateway and SDK that puts many
providers behind one OpenAI-style interface, with routing, keys, budgets and a management UI.
spendgate does less, on purpose. This page says which to pick and why, using only what we could
check. We read LiteLLM's source (the main branch as of 2026-11-11) while designing spendgate, and
re-read the documentation pages cited below on 2026-11-22. LiteLLM changes quickly, so check its
current documentation before relying on any line here.

## Short version

- **Choose LiteLLM** when you need breadth: many providers and models, rich routing (fallbacks
  and load balancing across models), and a full-featured proxy with an admin UI, and you can run
  the larger service that comes with it.
- **Choose spendgate** when the requirement is a budget that holds: a small codebase you can read
  in an afternoon, a design that refuses what it cannot price, and a stated, tested account of
  who pays when a call goes wrong.
- They are not exclusive. A guard in front of any OpenAI-compatible upstream is a legitimate setup
  if you accept that spendgate prices only what its catalog and adapters know; see
  [running spendgate in front of LiteLLM](#running-spendgate-in-front-of-litellm).

## Where they differ

Sources for the LiteLLM column: the source code read on 2026-11-11 (rows that say "we read"), and
the documentation pages [users and budgets](https://docs.litellm.ai/docs/proxy/users),
[alerting](https://docs.litellm.ai/docs/proxy/alerting),
[enterprise](https://docs.litellm.ai/docs/enterprise),
[caching](https://docs.litellm.ai/docs/proxy/caching) and
[reliability](https://docs.litellm.ai/docs/proxy/reliability) as they read on 2026-11-22 (rows that
say "documented").

| | LiteLLM | spendgate |
|---|---|---|
| Aim | One interface to many providers, plus a management layer | Hard spend limits for a few providers |
| Providers | Far more than spendgate | Seven adapters: OpenAI, Azure OpenAI, Anthropic, Bedrock, Google, Azure Speech, ElevenLabs |
| Routing | Fallbacks and balancing across deployments and models | Ordered deployments of one catalog id only; no cross-model fallback in v0.1 |
| Budget check | Reserves the worst case before the call and settles after; the admission check we read was an increment-then-compare, not an atomic check-and-set | One database transaction checks and holds every budget on the scope chain at once |
| Unpriced model | When we read it, a model without price data was not reserved against and was recorded at cost 0 | Refused with `model_not_priced` before any provider contact |
| Fail closed | Options exist (`fail_closed_*`) and were off by default | The default and not switchable by price data; opt-outs are explicit config keys, logged at start |
| Cancelled call | Settled to its input-token cost | Charged the full hold if the request was sent and no usage was seen |
| End-user (customer) budgets | Documented: a budget keyed on the request's `user` field, without a key per customer, shared across keys and teams ([users](https://docs.litellm.ai/docs/proxy/users)) | A `user:` scope with its own budget, reached through a key on that scope. The `X-Spendgate-User` header and the body's `user` field are labels for reports only and never choose a budget |
| Team budgets | Documented: team budgets, with per-member limits inside a team ([users](https://docs.litellm.ai/docs/proxy/users)) | No team object: a budget on any scope of the chain (organisation, project, service, user, session), and every budget on the chain is held at once |
| Budget windows | Documented: `budget_duration` resets a budget after a duration such as `30d`; resets are checked periodically ([users](https://docs.litellm.ai/docs/proxy/users)) | `period: none` (lifetime), `day` or `month`, reset at UTC calendar boundaries; no custom durations |
| Soft-budget alerts | Documented: a `soft_budget` alerts before the hard limit, sent to Slack and other webhooks ([alerting](https://docs.litellm.ai/docs/proxy/alerting)); soft-budget email alerts are listed as an enterprise feature ([enterprise](https://docs.litellm.ai/docs/enterprise)) | `soft_pct` (default 80) marks a budget past its threshold in `spendgate status` and the admin API; nothing is sent. Alert from the Prometheus metrics |
| Admin UI and SSO | Documented: an admin UI; SSO for it is free up to 5 users, then needs an enterprise licence ([enterprise](https://docs.litellm.ai/docs/enterprise)) | No UI and no SSO: a CLI and an [admin API](admin-openapi.yaml) on its own listener, with scoped admin keys |
| Guardrails and caching | Documented: guardrails (some open source, some enterprise; [enterprise](https://docs.litellm.ai/docs/enterprise)) and response caching ([caching](https://docs.litellm.ai/docs/proxy/caching)) | Out of scope. Nothing is cached and content is not inspected: every allowed request is forwarded, priced and charged |
| Budgets without a database server | Documented: budgets require a database, and the setup lists Postgres; without one, the global budget does not block ([users](https://docs.litellm.ai/docs/proxy/users)) | Budgets always live in a ledger, but for one host that is a SQLite file, and library mode needs no server at all. Postgres only to share a ledger |
| Size | A large codebase (one proxy module was about 23,000 lines at that commit) | At most six runtime dependencies; module and function size limits enforced in CI |
| Operating footprint | A proxy service with a database; the budget path we read used a Redis-style counter store, which several instances share | One process; SQLite for a host, Postgres to share; no Redis |
| Supply chain | Not assessed here | At most six runtime dependencies; PyPI release by trusted publishing (no token); container image built in CI from the tag, with SBOM and provenance, signed keyless with cosign; base images pinned by digest ([verifying the image](deploy/local.md#container-image)) |
| Telemetry | Not assessed here | None, ever |
| Maturity | Large user base and many integrations | New (v0.1), local deployments only, smaller test and user base |

The rows about LiteLLM's behaviour describe the code we read, or the documentation as it read, on
the dates above; they are the reason spendgate exists, not a claim that LiteLLM is unsuitable for
other jobs.

## What spendgate does not do

- Cover providers beyond its seven, or Realtime and hosted tools.
- Route across different models, or balance load by latency.
- Offer a web UI or SSO; administration is a CLI and an [admin API](admin-openapi.yaml).
- Send alerts itself (Slack, email, webhooks); it exposes metrics and status to alert from.
- Cache responses, or inspect content (guardrails, PII masking, moderation).
- Share rate limits (requests, tokens, concurrency) between instances; budgets are exact in the
  ledger, but those limits are per process in v0.1.
- Protect against whoever controls the host, or against code that holds a provider key (see
  [how money is protected](how-money-is-protected.md) and the [threat model](threat-model.md)).

## Running spendgate in front of LiteLLM

If you already run LiteLLM for breadth, spendgate can sit in front of it as the budget guard: apps
call spendgate with a spendgate key, and spendgate calls LiteLLM's OpenAI-compatible endpoint as if
it were OpenAI. spendgate holds and settles each call; LiteLLM picks the provider. Three things
keep the money right:

1. **Point the `openai` provider at LiteLLM.** Set `providers.openai.base_url` to LiteLLM's
   OpenAI-compatible root and `credential` to a LiteLLM key. The URL must be `https`, or `http`
   on loopback only.
2. **Price each route with an override entry carrying the real model's prices.** spendgate refuses
   a model it cannot price, and LiteLLM's model group names are not in the bundled catalog. Write
   one [override entry](catalog-how-to.md) per group you route, named `openai/<group name>`, with
   the prices and output limit of the model the group really calls (copy them from that model's
   bundled entry or its provider's page, and set `source_url` to that page). If a group can reach
   several models, price it at the most expensive one: spendgate cannot see which one LiteLLM
   chose.
3. **Disable LiteLLM's cross-model fallbacks for those routes.** A fallback to another model is
   billed at that model's price while spendgate charges the price of the route. Leave the groups
   out of LiteLLM's `fallbacks`, `context_window_fallbacks`, `content_policy_fallbacks` and
   `default_fallbacks`, or set `disable_fallbacks: true` in the metadata of the LiteLLM key that
   spendgate uses ([reliability](https://docs.litellm.ai/docs/proxy/reliability)). The
   per-request form does not help here: spendgate refuses body fields outside its allowlist, so a
   client cannot send it through.

A sketch with made-up numbers and names (`team-chat` is a LiteLLM model group):

```yaml
# litellm-prices.yaml: one entry per LiteLLM model group, at the real model's prices
catalog_version: 1
models:
  - id: openai/team-chat
    mode: chat
    currency: USD
    effective_from: "2026-11-22"
    source_url: https://example.com/pricing   # the real model's provider page
    verified: false
    checked_on: "2026-11-22"
    meters: {input_per_mtok: "1.50", output_per_mtok: "6"}
    limits: {max_output_tokens: 16384}
```

```yaml
# in spendgate.yaml
catalog: {paths: [./litellm-prices.yaml]}
providers:
  openai: {base_url: "https://litellm.internal.example/v1", credential: "env:LITELLM_API_KEY"}
models:
  team-chat: {catalog_id: openai/team-chat, deployments: [{provider: openai}]}
```

Settlement uses the usage LiteLLM returns, priced with your entry. What spendgate cannot see stays
outside its guarantee: a call LiteLLM retries upstream is booked once, and a price copied wrong is
charged wrong. Keep LiteLLM's own retries low, and keep LiteLLM on spendgate's side of the
perimeter (only spendgate may reach it), so apps cannot call LiteLLM directly.

## Switching

Both expose OpenAI-compatible endpoints (spendgate also Anthropic's), so moving a client is
mostly a base URL and key change. Model names, spend history and keys do not carry over: map your
models to catalog ids in `spendgate.yaml` and create new virtual keys.
