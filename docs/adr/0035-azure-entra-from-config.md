# ADR-0035: Entra ID for the Azure adapters is configured with `credential: entra`

**Status:** Accepted (extends [ADR-0017](0017-cloud-identity.md), which added `credential: adc`)
**Date:** 2026-11-28

## Context

Both Azure adapters can sign in with Microsoft Entra ID through `DefaultAzureCredential` (managed
identity, workload identity, environment, a developer's `az login`), and the CHANGELOG and the
adapter docstrings say so. The config could not reach it as intended (issue #141, pen test
SG-PT-16):

- `azure_openai` with `auth: bearer_token` and no `credential` was refused at load ("auth
  bearer_token needs a credential"). With a credential, the adapter sent that value as a static
  bearer token, so `serve` never used Entra ID for Azure OpenAI. Only library code that built a
  `Deployment` by hand could. A static Entra token read from a `file:` expires after about an hour
  and is read again only on `SIGHUP`, so this was no way to run Azure OpenAI on a managed identity,
  which is why ADR-0017 exists.
- `azure_speech` with `auth: bearer_token` also had to name a credential to load, and the adapter
  then ignored it and asked `DefaultAzureCredential`. An operator who put a token there believed it
  was used.

Google has the same split, and ADR-0017 solved it with a marker: `credential: adc` means "the
host's identity, no stored secret".

## Decision

- **`credential: entra`** on an `azure_openai` or `azure_speech` provider or deployment with
  `auth: bearer_token` means Entra ID from the host: the deployment holds no stored secret
  (`ambient`, as for `adc`) and the adapter gets its tokens from `DefaultAzureCredential` (the
  `azure` extra), with the cache, refresh and 401 rules of issue #140 (fetched in a worker thread,
  one fetch per scope at a time, dropped on a 401 or 403, an expired token refused, reused until the
  smaller of 5 minutes and half its life before expiry). `entra` with any other provider or auth
  mode is a config error naming the field (exit 3), as `adc` is outside Vertex AI.
- **Azure OpenAI keeps static bearer tokens.** A secret reference (`env:` or `file:`) with
  `auth: bearer_token` is still sent as a ready-made token, unchanged. A `Deployment` built by hand
  with no credential still means Entra ID, so library callers are unaffected.
- **Azure Speech bearer auth is Entra ID only, and anything else is refused.** An `azure_speech`
  deployment with `auth: bearer_token` and a credential other than `entra` stops `check-config` and
  `serve` (exit 3) at the field that names it, with a message that says to set `credential: entra`.
  The adapter also refuses (fails closed, nothing sent) a hand-built bearer `Deployment` that
  carries a stored token, instead of ignoring it.
- The error for a missing credential on these two adapters with `bearer_token` names `entra` as
  one of the choices.

## Alternatives considered

- **Make Azure Speech honour a static bearer token**, as Azure OpenAI does. It would make the two
  adapters alike, but it silently changes what an existing config does: today such a config works
  through Entra ID and its credential is ignored (often a placeholder), and after the change the
  gateway would start sending that value, so every call would fail with a 401 and nothing at load
  would say why. A static token is also of little use for Speech: the tokens its own `issueToken`
  endpoint hands out last 10 minutes, and an Entra token about an hour, while a `file:` secret is
  read again only on `SIGHUP`. Refusing at load is loud, names the field and says what to write.
- **Treat a missing credential as Entra ID.** It would load the configs that fail today, but an
  omitted key would then quietly mean "use whatever identity the host has", where every other
  provider treats a missing credential as an error. An explicit marker, like `adc`, keeps the
  choice visible in the file.
- **Reuse `adc` for Azure.** `adc` is Google's term (application default credentials) and its
  rules (project, scopes) are Google's; one marker per cloud reads better and keeps the checks
  simple.

## Consequences

- New accepted value for `credential` (provider and deployment): `entra`. No new key, error code,
  endpoint, log field, ledger change or dependency; `DefaultAzureCredential` is in the `azure`
  extra already used by Azure Speech and the `entra` ledger login.
- **Upgrade:** an `azure_speech` deployment (or `providers.azure_speech`) with `auth: bearer_token`
  and a secret-reference credential no longer loads (exit 3). Replace the credential with
  `credential: entra`; the gateway does what it did before, since that credential was never used.
  `azure_openai` configs load and behave as before.
- `SIGHUP` reload ignores `entra`, as it ignores `adc`: there is no file to read again.
- Documented in [operations.md](../operations.md#azure-with-entra-id) and
  [configuration.md](../configuration.md).
