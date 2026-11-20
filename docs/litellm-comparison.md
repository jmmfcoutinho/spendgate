# spendgate and LiteLLM

[LiteLLM](https://github.com/BerriAI/litellm) is a widely used gateway and SDK that puts many
providers behind one OpenAI-style interface, with routing, keys, budgets and a management UI.
spendgate does less, on purpose. This page says which to pick and why, using only what we could
check. We read LiteLLM's source (the main branch as of 2026-11-11) while designing spendgate;
LiteLLM changes quickly, so check its current documentation before relying on any line here.

## Short version

- **Choose LiteLLM** when you need breadth: many providers and models, rich routing (fallbacks
  and load balancing across models), and a full-featured proxy with an admin UI, and you can run
  the larger service that comes with it.
- **Choose spendgate** when the requirement is a budget that holds: a small codebase you can read
  in an afternoon, a design that refuses what it cannot price, and a stated, tested account of
  who pays when a call goes wrong.
- They are not exclusive. A guard in front of any OpenAI-compatible upstream is a legitimate setup
  if you accept that spendgate prices only what its catalog and adapters know.

## Where they differ

| | LiteLLM | spendgate |
|---|---|---|
| Aim | One interface to many providers, plus a management layer | Hard spend limits for a few providers |
| Providers | Far more than spendgate | Seven adapters: OpenAI, Azure OpenAI, Anthropic, Bedrock, Google, Azure Speech, ElevenLabs |
| Routing | Fallbacks and balancing across deployments and models | Ordered deployments of one catalog id only; no cross-model fallback in v0.1 |
| Budget check | Reserves the worst case before the call and settles after; the admission check we read was an increment-then-compare, not an atomic check-and-set | One database transaction checks and holds every budget on the scope chain at once |
| Unpriced model | When we read it, a model without price data was not reserved against and was recorded at cost 0 | Refused with `model_not_priced` before any provider contact |
| Fail closed | Options exist (`fail_closed_*`) and were off by default | The default and not switchable by price data; opt-outs are explicit config keys, logged at start |
| Cancelled call | Settled to its input-token cost | Charged the full hold if the request was sent and no usage was seen |
| Size | A large codebase (one proxy module was about 23,000 lines at that commit) | At most six runtime dependencies; module and function size limits enforced in CI |
| Operating footprint | A proxy service with a database; the budget path we read used a Redis-style counter store, which several instances share | One process; SQLite for a host, Postgres to share; no Redis |
| Telemetry | Not assessed here | None, ever |
| Maturity | Large user base and many integrations | New (v0.1), local deployments only, smaller test and user base |

The rows about LiteLLM's behaviour describe the code we read on the date above; they are the
reason spendgate exists, not a claim that LiteLLM is unsuitable for other jobs.

## What spendgate does not do

- Cover providers beyond its seven, or Realtime and hosted tools.
- Route across different models, or balance load by latency.
- Offer a web UI; administration is a CLI and an [admin API](admin-openapi.yaml).
- Share rate limits (requests, tokens, concurrency) between instances; budgets are exact in the
  ledger, but those limits are per process in v0.1.
- Protect against whoever controls the host, or against code that holds a provider key (see
  [how money is protected](how-money-is-protected.md) and the [threat model](threat-model.md)).

## Switching

Both expose OpenAI-compatible endpoints (spendgate also Anthropic's), so moving a client is
mostly a base URL and key change. Model names, spend history and keys do not carry over: map your models to catalog ids in
`spendgate.yaml` and create new virtual keys.
