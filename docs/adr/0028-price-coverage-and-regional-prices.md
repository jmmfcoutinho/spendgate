# ADR-0028: Price coverage, regional prices and how the catalog stays current

**Status:** Accepted; point 3 amended for Vertex AI by [ADR-0034](0034-vertex-unlisted-region.md)
**Date:** 2026-11-26

## Context

spendgate refuses a model it cannot price (`model_not_priced`, before any provider contact). That is
the guard working, but it also means a model a provider launches today, or a model sold in a region
the catalog does not know, is unusable until the catalog has it. The maintainer asked for prices for
every model and region the adapters can serve, and for a way to keep them current when a provider
launches a new model.

What the sources look like (read 2026-11-26):

- **Bedrock**: the AWS Price List bulk API is public and machine-readable for all 35 Regions. Prices
  depend on the profile (global, geographic, in-Region), not on the Region within a geography
  (#100).
- **Azure OpenAI**: the Azure Retail Prices API is public (no authentication) and lists every meter
  per Region under `serviceName eq 'Foundry Models'`: 46,380 meters in 51 Regions. Of the 1,474
  Azure OpenAI token meters sold in two or more Regions, **312 have different prices in different
  Regions** (a data-zone meter at 0.22 in some Regions and 0.24 in others, for example). Meter names
  are abbreviations ("gpt 4.1 nano cached Inp glbl Tokens"), so mapping them to catalog meters takes
  care.
- **Google Vertex AI**: the Google Cloud Pricing API lists public SKUs per Region. It needs an API
  key, which has no cost.
- **OpenAI, Anthropic and the Gemini API**: web pricing pages only. None of them publishes a price
  API or file.

What others do:
- **LiteLLM** keeps one community-maintained price file, and by default fetches it at runtime
  (`LITELLM_LOCAL_MODEL_COST_MAP` turns that off). Its documentation says a deployment whose
  provider has no matching row is billed at $0.
- **Langfuse** ships default prices, updates them with a daily audit that opens pull requests, and
  lets users add their own.
- **OpenRouter** publishes prices through an API.

The catalog cannot express a price that differs by Region today: an entry's `region` field is
documentation only, and a lookup takes a provider and a model, never a place.

## Decision

1. **An unpriced model stays refused.** No default price, no 0, no guess. The `model_not_priced`
   message names the model and points operators to the override file (`catalog.paths`) and to the
   stand-in command below. Catalog data still cannot fail open (M-10).

2. **Operators can price what the catalog lacks**, in an override file as today, and with a new
   helper, `spendgate catalog draft --like <catalog id> --as <catalog id> [--multiplier <m>]
   [--regions <slug,...>]`. It copies an existing entry's meters and limits, applies the multiplier
   (at least 1), and writes an override entry marked `verified: false` whose `note` names the entry
   it copied. It is an explicit, reviewed stand-in: `check-config` and `serve` already warn about
   every unverified price, and every call row records the price it used.

3. **Entries can be scoped to Regions.** A new optional field, `regions: [slug, ...]`, is enforced;
   the existing `region` stays documentation. A lookup takes the deployment's Region:
   - The candidates for an id are the entries in force (`effective_from <= today`).
   - If any candidate lists the deployment's Region in `regions`, only those candidates count.
     Otherwise only the candidates without `regions` count.
   - The newest `effective_from` among those wins, as before. No candidate means
     `model_not_priced`.
   - A deployment without a `region` matches only entries without `regions`. A Region-scoped entry
     is never used for a Region it does not list.
   - Two entries of one id with the same `effective_from` must not list the same Region (a load
     error), and at most one of them may omit `regions`.
   - The price id on a call row gains the Region when a Region-scoped entry priced it:
     `azure_openai/gpt-x@2026-11-01~swedencentral`. The ledger column is text, so its schema does
     not change.

4. **One route, one price.** Every deployment of a model route must resolve to the same entry.
   `check-config` and `serve` refuse a route whose deployments resolve to different entries, naming
   the route and the Regions. The hold and the charge then use one known price whichever deployment
   serves, as today, and failover never changes what a call costs. To route between
   differently-priced Regions, configure them as separate models. Pricing per deployment can come
   later, under its own decision.

5. **Prices come from the providers' own sources, through importers.** One maintainer script per
   provider (not shipped in the package, not run by the proxy) reads the official source and writes
   catalog entries. That means the AWS Price List; the Azure Retail Prices API; the Google Cloud
   Pricing API; and, for providers with web pages only, the page, cross-checked against LiteLLM's
   file at a pinned commit. Each importer is tested on recorded source data, and structural tests
   hold the bundled catalog to what the source showed: for example, every Bedrock standard price is
   1.1x its global price (`tests/unit/test_bedrock_catalog.py`). An entry from an official
   machine-readable source is `verified: true` once the maintainer has reviewed the pull request
   that adds it; a page-derived entry is `verified: true` once a person has compared it with the
   page, as now.

6. **A daily price-watch job proposes, a person decides.** A scheduled workflow on `main` runs the
   importers and opens (or updates) one pull request with new models and changed prices. It never
   merges. The Google API key it needs is a repository secret, given only to that scheduled job and
   never to a workflow that runs on pull requests. The proxy still fetches no prices at runtime
   (ADR-0014): a fetched price could be set to 0 by whoever controls the source. A merged price
   change ships in a patch release.

7. **Coverage target**: every model each adapter can serve, in every Region and deployment type the
   provider sells it in. That includes Bedrock's other model families through Converse, Azure
   OpenAI's global, data-zone and regional deployments in each Region, Vertex AI per Region, and the
   current models of OpenAI, Anthropic and the Gemini API.

## Consequences

- A new model is usable on the day it launches only through an operator override, a `catalog
  draft` stand-in or `spendgate catalog import` from LiteLLM, all marked unverified. The bundled
  price follows once the daily job's pull request is reviewed and released.
- An operator who routed one model across Regions that turn out to have different prices gets a
  start-up error after upgrading, and splits the route. That is a deliberate break: until now the
  hold silently used one Region's price for all.
- The catalog grows large (Azure alone is thousands of meters). Entries stay data, validated by the
  schema and the structural tests; the loader's cost is paid once at start.
- Importers depend on provider formats that can change. A format change makes the daily job fail
  loudly rather than propose wrong prices, because each importer refuses data it cannot map.
