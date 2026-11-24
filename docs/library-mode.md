# Library mode

Library mode gives a script or notebook the same budget rules as the proxy, without a server. An
`httpx` transport runs the same request pipeline in the calling process, against a local SQLite
ledger, and sends provider calls itself. Pricing, holds, refusals, failover, settlement and error
shapes are the proxy's code, not a copy.

## The limitation, plainly

Library mode is **cooperative, not a security boundary.** The process holds the provider key, so
any code in it (or any script that reads the same environment) can call the provider directly and
skip the guard. It protects honest code from mistakes: a runaway loop, a wrong model, a forgotten
batch job. For anything that must be enforced (shared keys, untrusted code, several teams), run
the proxy, which holds the keys so callers cannot bypass it ([threat model](threat-model.md)).

An autonomous agent that can run shell commands in the same account is code in that process for
this purpose, and it can undo the guard in three ways:

- **The ledger is its file.** The ledger is a SQLite file the process owns (0600, but owned by
  the same user), so the agent can delete it, edit balances in it, or point `ledger_path=` at a
  fresh one. Any of these undoes the cap.
- **The provider key is in its environment.** `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or the
  config's credential can be read with one command and used to call the provider directly.
- **There is no kill switch.** Library mode has no admin API and no kill switch; killing the
  process is the only way to stop it.

Do not give an autonomous agent with shell access a library-mode setup. Use the proxy with the
perimeter ([Running locally](deploy/local.md)): the agent then holds only a virtual key, the ledger
and provider keys live in another container, and an operator can stop it with the kill switch.
Run the agent inside the app container, not on the Docker host: an agent in the `docker` group can
`docker exec` into the gateway container and read its keys and ledger.

## Quick start

```python
import spendgate.local as sg

guard = sg.guard(cap="5 USD")      # create or open the ledger, scope and budget
client = guard.openai()            # OpenAI SDK; guard.anthropic() for Anthropic
client.chat.completions.create(
    model="gpt-6-luna", max_tokens=50, messages=[{"role": "user", "content": "hi"}]
)
```

Pass `max_tokens` (`max_output_tokens` for Responses) in every call. The guard holds the worst case
before sending, and without it that is the catalog's output limit for the model (128,000 tokens
for `gpt-6-luna`), so a small cap can refuse calls that would have cost a fraction of a cent.

`guard.openai(async_=True)` and `guard.anthropic(async_=True)` return the async SDK clients. Both
SDKs are optional extras (`openai`, `anthropic`). Use `with guard:` or `guard.close()` to release
the ledger connections (`await guard.aclose()` for async clients).

- **Routes.** With no config file, `guard` routes every model in the bundled catalog (plus a file
  passed as `catalog=`) of each provider whose credential is available: `OPENAI_API_KEY` for
  OpenAI, `ANTHROPIC_API_KEY` for Anthropic, or pass `provider_keys={"openai": "..."}`. No
  credential means an error that names both variables. Other providers (Azure, Bedrock, Google,
  ElevenLabs) need a config file: `guard(cap=..., config="spendgate.yaml")`. Its `bootstrap`
  section is not applied; the cap comes from `cap=`.
- **The cap** is a lifetime budget of the ledger file, created once. Asking for another cap on an
  existing ledger raises `CapMismatch` instead of changing it, because other scripts share it. Use
  another `ledger_path=` for a separate pool.
- **Currency.** The ledger currency is the cap's currency. The bundled catalog is USD, so
  `cap="5 USD"` needs no exchange rate. `cap="5 EUR"` needs
  `fx=FxSnapshot.of("USD", "EUR", "0.92", "2026-11-17", "ECB")` and otherwise fails at start with
  `no FX rate USD to EUR`. A currency cannot be changed on an existing ledger.
- **Calls in flight.** Each client (and each `transport()`) admits at most 16 calls at once, as
  the proxy does per key; more are refused with `rate_limited` (`details.limit: concurrency`)
  until one finishes. The bound also limits how far past the cap the budget can go when calls
  settle above their holds ([how money is protected](how-money-is-protected.md)). Change it with
  `guard(cap=..., concurrency=64)`; `concurrency=None` removes it. With `config=`, the file's
  `defaults.limits.concurrency` sets it instead (`null` removes it).
- **Ledger location.** `default_ledger_path()`: `$XDG_DATA_HOME/spendgate/local-ledger.db`
  (default `~/.local/share/...`) on Linux, `~/Library/Application Support/spendgate/` on macOS
  (POSIX only: setup uses `fcntl` file locks). The folder is created 0700 and the file 0600; a file
  with wider permissions is refused. Pass `ledger_path=` to put it elsewhere.

## Your own client: `transport()`

```python
import httpx
t = sg.transport("spendgate.yaml", ledger_path="spend.db", cap="5 USD")        # or async_=True
client = httpx.Client(transport=t, base_url="http://spendgate.local")
```

`transport(settings_or_path, *, ledger_path=None, async_=False, cap=None)` returns an
`httpx.BaseTransport` (or `AsyncBaseTransport`). Send `/v1/...` requests as you would to the
proxy; the caller's `Authorization` and `x-api-key` headers are dropped and the provider
credential from the config is used. The current OpenAI and Anthropic SDKs are built on `httpx2`
and refuse `httpx` objects: `guard.openai()` and `guard.anthropic()` handle that, and
`spendgate.local.transport_httpx2.transport2` is the same transport for your own `httpx2` clients.

Threading: the sync transport runs the pipeline on a private event loop thread, so it works in
plain scripts and inside Jupyter (where a loop is already running). The async transport runs on
your loop and must stay on it. Both keep their leases alive and settle holds left by crashed
scripts (charged in full, like the proxy).

## Sessions

```python
with sg.session("nightly-import", budget="2 USD", max_steps=200, max_seconds=3600):
    ...
```

`spendgate.local.session` is the library-mode form of `spendgate.client.session`, which refuses
limits because the proxy takes them from the key. Here the limits travel with each call into the
local principal's session defaults, and the ledger copies them onto the session the first time
its id is used. Later blocks with the same id keep the limits the session started with. A session
budget is its own cap inside the shared one; reaching it returns `session_limit_reached`.

## Several scripts, one machine

Scripts that name the same ledger file share one pool, and the cap holds across processes: the
ledger serialises writers with SQLite transactions (tested with several processes). It is a single
machine feature. Several machines need the proxy with PostgreSQL.

## What differs from proxy mode

| | Proxy | Library |
|---|---|---|
| Enforced against callers | yes (keys stay in the proxy) | no (cooperative) |
| Keys and per-key policy | stored, rotated, scoped | one local principal, no key store |
| Budgets | admin API | the `cap` (one org budget) and session limits |
| Metrics, admin API, kill switches | yes | no |
| Streaming | by the pipeline | by the pipeline (relayed through the transport) |
