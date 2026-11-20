# spendgate

**Status: pre-1.0.** The configuration format, error codes and ledger schema may change between
minor versions. v0.1 targets local deployments: one machine, Docker Compose, SQLite or Postgres.

spendgate is a small, auditable guard for LLM spending: an OpenAI- and Anthropic-compatible proxy
plus a Python SDK and a no-server library mode. Before a call is forwarded it holds the worst-case
cost in every budget the call belongs to, refuses anything it cannot price, and settles the real
cost exactly once afterwards.

## What it guarantees

[How money is protected](docs/how-money-is-protected.md) links each guarantee to the code and the
tests that prove it.

- **Fail closed.** An unknown or unpriced model, a missing exchange rate or output ceiling, an
  unreachable ledger, a kill switch or a bad key refuses the call before the provider is contacted.
  Price data cannot switch this off.
- **Hard holds.** The worst case is held before sending, in one transaction across the whole scope
  chain, so `spent + held` never exceeds the cap plus recorded overruns. A call is free only if it
  never reached the provider or the provider rejected it; anything else after sending is charged
  the full hold unless usage was seen.
- **Overruns are recorded, never clamped.** Usage above the hold is booked as reported. A breach of
  the cap blocks the budget until an operator acknowledges it (the default, `strict_overrun`).
- **Session limits.** A session id gets a budget, a step limit, a time limit and an idle expiry,
  so a runaway loop ends with `session_limit_reached`.
- **Throttling costs nothing.** A provider 429 cools that deployment down and the call goes to the
  next deployment of the same model inside the same reservation, at no charge.
- **A perimeter you can test.** In the reference Docker Compose setup only the proxy holds provider
  keys and has internet access; apps on the internal network cannot reach a provider at all.

Library mode is cooperative: code that holds the provider key can skip it. Use the proxy with the
perimeter when something must be enforced. See [Running locally](docs/deploy/local.md).

## Quickstart: proxy mode

Install with the SDK you use (`openai`, `anthropic`), then write a config. Providers, prices and
budgets are data; this one routes one model and gives it a 5 USD lifetime cap.

```bash
pip install "spendgate[openai]"
```

<!-- doctest: file spendgate.yaml -->
```yaml
server: {listen: "127.0.0.1:8080", key_pepper: "env:SPENDGATE_KEY_PEPPER"}
ledger: {currency: USD, backend: {kind: sqlite, path: ./spendgate.db}}
providers:
  openai: {base_url: "https://api.openai.com/v1", credential: "env:OPENAI_API_KEY"}
models:
  gpt-6-luna: {catalog_id: openai/gpt-6-luna, deployments: [{provider: openai}]}
bootstrap:
  scopes: ["org:demo"]
  budgets: [{id: demo, scope: "org:demo", period: none, cap: "5 USD"}]
  keys: [{name: quickstart, scope: "org:demo"}]
```

Check it, create the virtual key, then start the proxy. The provider key stays here, in the
proxy's environment. `migrate --bootstrap` prints each key once; `serve` never prints a secret, so
none ends up in a container or service log.

<!-- doctest: shell -->
```bash
export OPENAI_API_KEY=sk-...    # your real provider key
export SPENDGATE_KEY_PEPPER=$(openssl rand -hex 32)    # keep it: it protects stored keys
spendgate check-config --config spendgate.yaml
spendgate migrate --config spendgate.yaml --bootstrap
# bootstrap: created api key quickstart (org:demo): sg-...   <- printed once; copy it
```

<!-- doctest: serve -->
```bash
spendgate serve --config spendgate.yaml
```

In another terminal, call it with the virtual key (`sg-...`), never the provider key. The helper
sets the base URL and attribution headers; any unmodified OpenAI or Anthropic SDK also works.

<!-- doctest: python -->
```python
import spendgate.client as sg

client = sg.openai("http://127.0.0.1:8080/v1", "sg-...", user="u-42", tags=["quickstart"])
with sg.raise_for_spendgate():  # BudgetExhausted, RateLimited, ModelNotPriced, ...
    reply = client.chat.completions.create(
        model="gpt-6-luna", max_tokens=50, messages=[{"role": "user", "content": "hi"}]
    )
print(reply.choices[0].message.content)
```

See who spent what:

<!-- doctest: shell -->
```bash
spendgate usage --local --config spendgate.yaml --by model
```

Next: [running locally with the perimeter](docs/deploy/local.md), [operations](docs/operations.md)
(keys, kill switch, backups) and the [configuration reference](docs/configuration.md).

## Quickstart: library mode

No server and no key store: an `httpx` transport runs the same pipeline in your process against a
local SQLite ledger.

<!-- doctest: library -->
```python
import spendgate.local as sg

client = sg.guard(cap="5 USD").openai()   # needs OPENAI_API_KEY; ledger in your user data dir
client.chat.completions.create(model="gpt-6-luna", messages=[{"role": "user", "content": "hi"}])
```

The cap lasts for the life of the ledger file and is shared by every script that opens it.
[Details and limits](docs/library-mode.md).

## Feature matrix

Every row goes through the same hold, refusal and settlement rules. "stream" means the endpoint
streams and the stream is settled from its usage.

| Provider | Chat completions | Responses | Messages | Embeddings | Transcription | Speech |
|---|---|---|---|---|---|---|
| OpenAI | yes, stream | yes, stream | | yes | yes | yes, chunked |
| Azure OpenAI | yes, stream | yes, stream | | yes | yes | |
| Anthropic | | | yes, stream | | | |
| AWS Bedrock | yes, stream (translated) | | | | | |
| Google (Gemini API, Vertex AI) | yes, stream (translated) | | | | | |
| Azure Speech | | | | | yes | |
| ElevenLabs | | | | | | yes, chunked |

Endpoints are the OpenAI paths (`/v1/chat/completions`, `/v1/responses`, `/v1/embeddings`,
`/v1/audio/transcriptions`, `/v1/audio/speech`) and Anthropic's `/v1/messages`. Fields outside the
allowlist are refused rather than forwarded; see the [wire protocol](docs/protocol.md). Realtime
and hosted tools are not supported. Backends: SQLite (one host) and Postgres (shared).

## Documentation

- [How money is protected](docs/how-money-is-protected.md): each guarantee with its code and test.
- [Documentation index](docs/README.md), [configuration reference](docs/configuration.md),
  [operations](docs/operations.md).
- How to [add a model](docs/catalog-how-to.md) or [a provider](docs/provider-how-to.md).
- [spendgate and LiteLLM](docs/litellm-comparison.md): when to use which.
- [Roadmap](ROADMAP.md): what may come next.
- [Threat model](docs/threat-model.md), [contributing](CONTRIBUTING.md), [security policy](SECURITY.md).

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13 or newer.

```bash
uv sync --all-extras
make check   # every quality gate
```

## Licence

[Apache-2.0](LICENSE).
