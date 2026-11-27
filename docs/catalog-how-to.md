# How to add a model to the catalog

Prices are data. A new model, or a new price for an existing one, is a YAML edit; no code changes.
The format is described in [catalog-schema.md](catalog-schema.md) and enforced by
[`src/spendgate/catalog/schema.json`](../src/spendgate/catalog/schema.json). This page is the
short path. A model that has no price in force is refused with `model_not_priced`, so a missing
entry is safe, never free; [section 9](#9-when-a-model-has-no-price) says how to price it, or to
stand it in with `spendgate catalog draft` until its price is known.

## 1. Write the entry

Read the provider's own pricing page and write every price as a quoted decimal string, per the
unit in the field name (`_per_mtok` is per million tokens). This is an illustrative entry with
made-up numbers:

<!-- doctest: catalog -->
```yaml
catalog_version: 1
models:
  - id: openai/example-model            # provider slug / the name callers send
    mode: chat                          # chat, responses, messages, embedding, transcription, speech
    currency: USD
    effective_from: "2026-11-17"        # first day this entry applies (UTC)
    source_url: https://example.com/pricing   # the provider's official page
    verified: false                     # see below
    checked_on: "2026-11-17"
    meters: {input_per_mtok: "1.50", cached_input_per_mtok: "0.15", output_per_mtok: "6"}
    limits: {max_output_tokens: 16384}  # required for chat, responses and messages
```

The rules that trip people up:

- **Prices are strings.** A YAML float (`1.5`) is rejected, because floats cannot hold money.
- **Mode decides the mandatory meters.** chat, responses and messages need `input_per_mtok` and
  `output_per_mtok`; embedding needs `input_per_mtok`; transcription needs
  `audio_input_per_minute` or `audio_input_per_mtok`; speech needs `per_1k_characters` or
  `audio_output_per_mtok`.
- **`limits.max_output_tokens` is the last-resort output ceiling.** The worst-case hold uses it when
  neither the request nor the key sets one. Without it a chat entry is invalid, because the guard
  refuses to guess the largest possible bill. A transcription entry with an output meter (a
  transcript billed per token, such as gpt-4o-transcribe) needs it too, set to the model's
  documented output limit: no transcription request field bounds the transcript, so the hold takes
  this value whatever the key sets.
- **The provider slug in `id` is an adapter name:** `openai`, `azure_openai`, `anthropic`,
  `bedrock`, `google`, `azure_speech` or `elevenlabs`.
- **Currency.** If it differs from `ledger.currency`, the config needs an `fx:` rate or startup
  fails ([configuration](configuration.md#fx)).
- **Long context and multipliers.** `long_context` reprices a whole request above a token
  threshold; `price_multiplier` scales every meter (for example a data-zone uplift). Both are
  per entry.

## 2. `verified` and `source_url`

`source_url` is required and must be the provider's official page for that price. `verified` says
whether a person compared every price on the entry with that page. Set `true` only after doing so,
and set `checked_on` to that day. Leave `false` for anything copied from a feed, a blog or memory:
the guard still prices with it, but nobody should treat it as audited. A wrong price is the one
error the ledger cannot detect; the overrun record is the safety net, not a substitute.

Unverified prices are never silent. When the effective catalog (bundled plus your overrides)
holds entries with `verified: false`, `spendgate serve` and library mode log one warning at start
naming the count and the first few ids, and `spendgate check-config` prints the same line.
`check-config` also warns when an unverified entry supersedes a verified one of the same id (a
later `effective_from`, or the same one in a later file): from that date the reviewed price no
longer applies.

## 3. Load it

Put the entry in a file of your own and list it in the config:

```yaml
catalog:
  paths: [./my-prices.yaml]
```

An override is applied after the bundled catalog and replaces an entry with the same `id` and
`effective_from` (and the same `regions`, section 8); anything else is added. Then route the model in `models:`:

```yaml
models:
  example: {catalog_id: openai/example-model, deployments: [{provider: openai}]}
```

Check without starting the proxy:

```bash
spendgate check-config --config spendgate.yaml
```

It fails on a bad entry (naming the file and field, never the value), and when a routed model has
no price in force it fails with one error naming the model (`models.<alias>.catalog_id`). In library mode, pass the file as `catalog=` and every priced model of a
provider you hold a key for is routed.

## 4. Changing a price or retiring a model

- **New price:** add a second entry with the same `id` and a later `effective_from`. The newest
  entry in force wins; the older one stays for history.
- **Retire:** set `deprecation_date` to the provider's shutdown date. From that day the model is
  refused, with no fallback to an older entry.
- Every call row records the catalog checksum and the entry used, so a past charge can be
  explained after prices change.

## 5. Contributing a bundled model

Edit [`src/spendgate/catalog/prices.yaml`](../src/spendgate/catalog/prices.yaml) and run
`uv run pytest tests/unit/test_catalog.py tests/unit/test_catalog_schema.py`. The schema test
checks the shipped file with both the JSON Schema and the Pydantic models. Say in the pull request
which page you read and when.

## 6. Starting from LiteLLM's price file, and cross-checking against it

LiteLLM publishes a community price file. It is a useful starting point and a second opinion, but
spendgate never reads it at runtime. The proxy makes no remote fetches by design
([catalog-schema.md](catalog-schema.md)): a price file pulled over the network would let whoever
controls that file, or the path to it, set any price to 0 and switch the guard off. LiteLLM is a
third party and has had a supply-chain compromise, so the bundled catalog stays a reviewed,
dated, sourced file, and the tooling below is for operators and maintainers at their desk.

**An override file for an operator: `spendgate catalog import`.** It drafts a catalog override
file of every mappable LiteLLM model that the bundled catalog does not carry
([ADR-0014](adr/0014-catalog-import-command.md)):

```bash
# Download LiteLLM's model_prices_and_context_window.json at one commit (full 40-hex SHA).
spendgate catalog import --from-litellm <commit-sha> --out extra.yaml

# Offline: read a copy you already have (add --from-litellm <sha> to record which commit it is).
spendgate catalog import --from-file model_prices_and_context_window.json --out extra.yaml

# Only some providers; overwrite an earlier draft.
spendgate catalog import --from-litellm <commit-sha> --out extra.yaml --providers openai,google --force
```

- **Pinned, never live.** `--from-litellm` takes a full commit SHA, never a branch, and fetches
  only `https://raw.githubusercontent.com/BerriAI/litellm/<sha>/model_prices_and_context_window.json`
  (no redirects, at most 32 MiB). Nothing is fetched at serve time; the command is the only code
  in spendgate that downloads anything, and it runs only when you run it.
- **Every entry is `verified: false`**, with `effective_from` and `checked_on` set to today and
  `source_url` set to the pinned file. The file header records the SHA-256 of the bytes read.
- **Bundled ids are left alone, at draft time.** A model the installed bundled catalog prices is
  not drafted. A later spendgate release may bundle a model you drafted; your drafted entry is
  then dated later and wins. `check-config` warns about it (section 2). After upgrading, re-run
  the import into a fresh file, or delete the drafted entries the release now carries.
- **Bad entries are skipped, one by one.** Each skip is a `skipped '<key>': <reason>` line on
  stderr, naming no value. Skipped: a value that is not a finite number (NaN, a string, a
  boolean, an absurd exponent), a price of 0 on any meter, a long-context multiplier below 1, a
  chat, responses or messages model without a whole `max_output_tokens` (never a guessed or
  rounded ceiling), and anything that would not load (such as a transcription model with an output
  price and no `max_output_tokens`). The finished file is checked against the
  bundled catalog before it is written.
- **Never overwrites** an existing `--out` unless you pass `--force`, which replaces it in one
  rename, so a failure leaves the old file whole.
- Exit codes: 0 written; 2 bad command line or the file exists; 3 the input is not LiteLLM's
  JSON or nothing could be drafted; 5 the download failed.

Then load it like any override (`catalog.paths`, section 3) and run `spendgate check-config`: the
start-up warning in section 2 lists the unverified entries. The risk is yours: a stale or tampered
number there is a wrong charge in your ledger. Read it first, prefer a handful of checked entries,
and do section 2 for each model you rely on.

**Maintainer tooling.** [`scripts/litellm_prices.py`](../scripts/litellm_prices.py) shares the
command's mapping and reads LiteLLM's
`model_prices_and_context_window_backup.json` from one pinned commit of `BerriAI/litellm` (the
default is hard-coded in the script; `--commit <sha>` picks another, `--file <path>` reads a local
copy). It records the SHA-256 of the bytes it read and never writes into `prices.yaml`.

```bash
# Compare our catalog with LiteLLM: equal, different (both values, % and direction), ours only, theirs only.
uv run python scripts/litellm_prices.py diff
uv run python scripts/litellm_prices.py diff --commit <sha> --threshold 0.5 --out report.txt

# Draft entries for models we do not carry yet (prints YAML; --out writes a file).
uv run python scripts/litellm_prices.py import gpt-6-luna gemini/gemini-4-carbon --out draft.yaml

# The same kind of override file as `spendgate catalog import`, from the script's pinned commit.
uv run python scripts/litellm_prices.py override --out litellm-extra.yaml
```

**Adding a model with it.** `import` converts the named LiteLLM entries into our format with exact
decimal arithmetic (per-token floats become per-MTok strings; nothing goes through binary floats),
validates the result against the schema, and sets `verified: false`, `checked_on` to today and
`source_url` to the file at the pinned commit. Then do section 2 by hand: open the provider's own
page, compare every price, replace `source_url` with that page, and only then set `verified: true`.
Paste the entry into `prices.yaml` yourself.

**Reading the diff.** The exit code is 1 when a price we carry differs from LiteLLM's by more than
the threshold (default 1%), when a long-context price or threshold differs, or when LiteLLM lists a
deprecation date within the next 180 days (`--horizon-days`) that we do not carry. A difference is
a question, not a verdict: either file can be wrong. Like is compared with like. Azure entries store
Global Standard meters and a `price_multiplier`: the meters are compared with LiteLLM's global key,
and the multiplier is then checked as a ceiling against the dearest Data Zone price LiteLLM lists.
Anthropic and Bedrock cache writes are compared with the 1-hour price, which is what the catalog
stores. Entries that deliberately hold an upper bound (the highest regional meter, say) show up as
"ours HIGHER"; read the entry's note before touching them.

**Weekly check.** [`.github/workflows/prices.yml`](../.github/workflows/prices.yml) runs `diff`
against LiteLLM's latest `main` commit every Monday, fails when prices disagree and uploads the
report as an artifact. The daily price watch (section 12) puts the same diff's summary in its pull
request, without failing on it.

**The script's `override` command** writes the same kind of file from the script's pinned commit;
operators use `spendgate catalog import` instead, which needs no checkout of this repository.

## 7. AWS Bedrock: which catalog id to use, and how the entries are made

On Bedrock most models are called through **inference profiles**: the model id is prefixed with a
geography (`eu.anthropic.claude-haiku-5-5`) or `global.`. Some are served **in-Region** under the bare id
(`mistral.mistral-large-3-675b-instruct`), and some both ways. The bundled catalog prices each id AWS's model
cards list on bedrock-runtime, for every text model the Converse adapter can serve, so name the model by the id
you call and leave the deployment's `model` unset. Always set the deployment's `region` (the Region in
`base_url`): many Bedrock prices depend on it.

```yaml
providers:
  bedrock: { base_url: https://bedrock-runtime.eu-west-1.amazonaws.com, auth: sigv4, region: eu-west-1 }
models:
  claude-haiku:
    catalog_id: bedrock/eu.anthropic.claude-haiku-5-5   # the profile id is also what Bedrock is sent
    deployments: [{ provider: bedrock }]
```

What is bundled (AWS Price List and model cards read 2026-11-26; "in-Region" = the bare id):

| Family | Models | Ids |
|---|---|---|
| Anthropic Claude 5.x | Fable 5, 5.1, 5.2; Mythos 5.1, 5.2; Haiku 5.5; Sonnet 5, 5.5; Opus 5, 5.5 | `us.` and `global.` for all; Haiku 5.5 also `ca.` `eu.` `au.` `jp.`; Sonnet 5.5 `ca.` `eu.` `au.`; Opus 5.5 `eu.` `au.` `jp.` and in-Region (eu-west-2); Sonnet 5 and Opus 5 `eu.` `au.` `in.` |
| Anthropic Claude 4.x | Haiku 4.5, Sonnet 4.5, 4.6, Opus 4.1, 4.5, 4.6, 4.7, 4.8 | `us.` (all), `eu.`, `au.`, `jp.`, `in.` and `global.` as each card lists |
| Amazon Nova | Nova 2 Lite, Nova Pro, Lite, Micro | `us.` `eu.` (`apac.` `ca.` `jp.` `global.` where offered), in-Region |
| Meta Llama | Llama 3 8B, 70B; 3.1 8B, 70B; 3.3 70B; 4 Scout, Maverick | in-Region and `us.` |
| Mistral AI | Mistral 7B, Large (2402), Large 3, Small (2402), Mixtral 8x7B, Ministral 3B, 8B, 14B, Magistral Small, Devstral 2, Pixtral Large | in-Region; Pixtral `us.` `eu.` |
| OpenAI | GPT-5.4, 5.5, 5.6 Luna, Sol, Terra; GPT-6 Astra, Luna, Sol; 6.1 Sol, Terra; 6.2 Astra, Sol | `us.`, `global.` (not 5.4, 5.5, 6.1 Sol: see below), `in.` for 5.6 Luna and Terra |
| OpenAI open weights | gpt-oss 20B, 120B, gpt-oss-safeguard 20B, 120B | in-Region, `us-gov.` for gpt-oss |
| Others | DeepSeek R1, V3.1, V3.2; Gemma 3 4B, 12B, 27B; MiniMax M2, M2.1, M2.5; Kimi K2 Thinking, K2.5; Nemotron Nano 9B v2, 12B v2 VL, Nano 3 30B, Super 3 120B; Qwen3 32B, 235B, Coder 30B, 480B, Coder Next, Next 80B, VL 235B; Writer Palmyra X4, X5, Vision 7B; GLM 4.7, 4.7 Flash, 5 | in-Region (DeepSeek R1, Palmyra X4 and X5: `us.`; Nemotron: also `us-gov.`) |

- **The price follows the Region you call from.** A geographic profile and an in-Region id cost the Standard price
  of the source Region; `global.` costs its global price (for Claude about 10% less than the geographic ones,
  and it may route to any commercial Region: use it only if your data may leave your geography). Where all source
  Regions have the same price the entry has no `regions`; otherwise there is one Region-scoped entry per price
  (section 8), so a route spanning two of them must be split. A source Region where AWS lists no price has no
  entry, and calls from there are refused (`model_not_priced`), for example `global.anthropic.claude-sonnet-5-5`
  from ap-northeast-1. The `us.` profile also accepts the Canadian Regions as source.
- **Not priced, and why** (each listed in `scripts/bedrock_models.yaml`): models whose card names no maximum output
  tokens (GPT-5.6 Cyber, Daybreak Blue, Voxtral, Kimi K3, Grok 4.6), models priced only on the bedrock-mantle
  endpoint (Grok 4.7, Kimi K3), no Converse (embeddings, image, video, speech, Gemma 4, Pegasus), no model card
  (Nova 2 Pro and Omni, Nova Premier, Claude 3.x and older, Titan Text) and models at or past end of life (Llama 3.1
  405B and 3.2, Command R, Jamba 1.5, Claude Sonnet 4 and 3 Haiku). Ids a card lists but the Price List prices in
  no source Region are left out too: `global.openai.gpt-5.4`, `global.openai.gpt-5.5` and the `us-gov.` OpenAI GPT
  profiles; `global.openai.gpt-6.1-sol`'s card lists no source Regions. Flex, Priority, batch, latency-optimized and
  Ultrafast prices are not encoded: the adapter never asks for them. Add an override file (section 1) for anything
  else; pointing a catalog id at a different model with a deployment `model:` charges that entry's price.
- **Updating.** The entries live between the `>>> generated by scripts/bedrock_prices.py` markers in
  `prices.yaml`; do not edit them by hand. They come from the AWS Price List bulk API (offers
  `AmazonBedrockFoundationModels` and `AmazonBedrock`, `region_index.json` and every Region file) and from
  `scripts/bedrock_models.yaml`, the reviewed table of what the price list does not say (ids, profiles and their
  source Regions, limits, long-context thresholds, retirement dates, from AWS's model cards):

  ```bash
  uv run python scripts/bedrock_prices.py --download /tmp/aws-prices    # a new, empty directory
  uv run python scripts/bedrock_prices.py --from-dir /tmp/aws-prices --write
  uv run python scripts/bedrock_prices.py --from-dir /tmp/aws-prices --check   # exit 1 when out of date
  ```

  It downloads only over https from `pricing.us-east-1.amazonaws.com`, follows no redirect and caps each file at
  32 MiB. It stops (exit 2) on any usage type it cannot map, on a model the table neither maps nor skips, and on
  prices the catalog cannot express, rather than skipping them silently: a new model means a reviewed change to the
  table, read from its model card. `tests/unit/test_bedrock_catalog.py` holds the Claude 5.x entries to AWS's
  structure (every standard price 1.1x the global one) and the profile set per model to the model cards.

## 8. Prices that differ by Region

Some providers charge a different price for the same model in different Regions (Azure OpenAI does for
many data-zone and regional deployments). A catalog entry can be scoped to the Regions it prices with
`regions` ([ADR-0028](adr/0028-price-coverage-and-regional-prices.md)). The deployment's `region` in
`spendgate.yaml` (its own, or its provider's) picks the entry. Write the Region-scoped entry with the same
`id` as the region-less one, in your override file (illustrative numbers):

<!-- doctest: catalog-regions -->
```yaml
catalog_version: 1
models:
  - id: azure_openai/example-model      # every Region not listed below
    mode: chat
    currency: USD
    effective_from: "2026-11-01"
    source_url: https://example.com/pricing
    verified: false
    meters: {input_per_mtok: "2", output_per_mtok: "8"}
    limits: {max_output_tokens: 16384}
  - id: azure_openai/example-model      # these two Regions only
    mode: chat
    currency: USD
    effective_from: "2026-11-01"
    regions: [swedencentral, westeurope]
    source_url: https://example.com/pricing
    verified: false
    meters: {input_per_mtok: "2.2", output_per_mtok: "8.8"}
    limits: {max_output_tokens: 16384}
```

- **A listed Region takes the Region-scoped entry**, the newest in force among those that list it; it
  never falls back to a region-less entry while one is in force. **Every other Region**, and a deployment
  without a `region`, takes the newest region-less entry in force. If there is none, the call is refused
  with `model_not_priced`: a Region-scoped price is never used for a Region it does not list.
- `region` (singular) stays documentation only; `regions` is the one that is enforced. Region names are
  compared as written, so use the provider's own slug (`swedencentral`, `eu-west-1`) in both files.
  `check-config` and `serve` warn about a deployment Region that no entry of its Region-scoped id lists
  (it takes the region-less price), naming the Regions the catalog does list, so a typo shows at start.
- Entries of one `id` and `effective_from` must not list the same Region twice, and at most one of them
  may omit `regions`: either is a load error. An override replaces the entry with the same `id`,
  `effective_from` and set of `regions`; one whose Regions only overlap is refused.
- The call row's `price_id` gains the Region when a Region-scoped entry priced the call:
  `azure_openai/example-model@2026-11-01~swedencentral`. A region-less entry's id is unchanged.

**One route, one price.** Every deployment of a model in `models:` must resolve to the same entry, so the
hold and the charge are the same whichever deployment serves and failover never changes what a call
costs. `check-config` and `serve` refuse (exit 3) a route whose deployments resolve to different entries,
today or from a later `effective_from` already in the catalog, naming the route, each Region and the
price it would get. Deployments in `swedencentral` and `westeurope` above can share a route; a deployment
in `eastus` cannot join them. Route differently-priced Regions as separate models:

```yaml
models:
  example-eu: {catalog_id: azure_openai/example-model, deployments: [{provider: azure_openai, region: swedencentral}, {provider: azure_openai, region: westeurope}]}
  example-us: {catalog_id: azure_openai/example-model, deployments: [{provider: azure_openai, region: eastus}]}
```

## 9. When a model has no price

A model with no price in force is never guessed, never free: `check-config` and `serve` refuse a
route to it, and a call is refused with `model_not_priced` before any provider contact. The
message names the model and the two ways to price it:

- **The provider's price, in an override file** (sections 1 to 3). This is the real fix.
- **A stand-in until then: `spendgate catalog draft`.** It copies the price of a model the catalog
  already prices, of the same provider, onto the new id
  ([ADR-0028](adr/0028-price-coverage-and-regional-prices.md)):

```bash
# Price a new Anthropic model like Claude Haiku 5.5, 25% dearer, until its own price is known.
spendgate catalog draft --like anthropic/claude-haiku-5-5 --as anthropic/claude-new-model \
  --multiplier 1.25 --out stand-in.yaml

# Copy from your own override files too, not only the bundled catalog.
spendgate catalog draft --like openai/my-model --as openai/my-model-2 --config spendgate.yaml --out stand-in.yaml

# A stand-in for two Regions only (section 8), copied from the price my-model has in swedencentral.
spendgate catalog draft --like azure_openai/my-model --as azure_openai/my-model-2 --config spendgate.yaml \
  --region swedencentral --regions swedencentral,westeurope --multiplier 1.1 --out stand-in-eu.yaml
```

- **What is copied.** The `--like` entry in force today (UTC): every price meter times
  `--multiplier`, exactly (a product that would need rounding is refused), plus `mode`,
  `currency`, `price_multiplier`, `long_context` and `limits` as they are, and its `source_url`.
  `region`, `regions`, `deployment_type` and `deprecation_date` belong to the copied model and
  are left out. If the `--like` model has a scheduled price (an entry with a later
  `effective_from`), the draft follows it with one entry per date.
- **`--regions a,b`** scopes the stand-in to those Regions (section 8): it writes `regions:`,
  sorted and without repeats, and the note says `for Regions a, b`. Every other Region, and a
  deployment without a `region`, keeps whatever price the id already has, or none. A slug is
  lower-case letters, digits and `-`, as the schema requires. A Region that another entry of the
  same id and `effective_from` already lists is refused (exit 3), because lookup must find one
  entry per Region; an entry with exactly the same Regions is replaced, as any override is.
- **`--region <slug>`** chooses which price of a Region-scoped `--like` is copied: the one a
  deployment in that Region would pay, scheduled changes in that Region included, and the note
  names it (`…@2026-11-01~swedencentral`). Without it, the region-less price is copied; a
  `--like` priced only for some Regions is refused, and the error lists them. `--region` picks the
  source only: the stand-in is scoped by `--regions` alone.
- **The entry is `verified: false`**, with `effective_from` and `checked_on` today and a `note`
  such as `Stand-in: priced like anthropic/claude-haiku-5-5@2026-11-13 x 1.25; replace with the
  provider's price`. The start-up warning (section 2) lists it, and every call row records its
  price id, so a charge made with a stand-in is never mistaken for a checked one.
- **`--multiplier`** (default 1) is a plain decimal of at least 1: a stand-in can cost more than
  the model it copies, never less. Pick it from what you know of the new model's price; when in
  doubt, higher.
- **Refused:** a `--like` with no price in force (an unknown id, or one not yet in force, or
  none in that `--region`), two ids of different providers, a malformed id or Region slug, a
  multiplier below 1 or not a decimal, and an existing `--out` without `--force` (which replaces
  the file in one rename).
- **`--as` may already be priced.** Then the stand-in supersedes that price from today (in each
  of `--regions`, if given), and the command warns on stderr; `check-config` warns too when the
  price it replaces was verified.
- **Reads:** the bundled catalog, plus the override files in `catalog.paths` when `--config` is
  given (no secret is read). Exit codes: 0 written; 2 bad command line, an unknown `--like` or the
  file exists; 3 the configuration cannot be read or the draft would not load.

Load the file with `catalog.paths` (section 3) and run `spendgate check-config`. Replace the entry
with the provider's own price as soon as it is published.

## 10. Azure OpenAI: which catalog id to use for your deployment

Azure OpenAI charges by deployment type (Global Standard, Data Zone Standard, Regional Standard)
and, for many models, by Region too. The bundled catalog has one id per deployment type, generated
from the Azure Retail Prices API by [`scripts/azure_prices.py`](../scripts/azure_prices.py):

| Deployment type in Azure | Catalog id | Typical price |
|---|---|---|
| Global Standard | `azure_openai/global.<model>` | the base price, the same in every Region for almost all models |
| Data Zone Standard | `azure_openai/datazone.<model>` | typically 10% to 20% above Global, depending on the zone |
| Regional Standard | `azure_openai/regional.<model>` | Region-dependent; only some older models |

`<model>` is Azure's model name (`gpt-6-luna`, `gpt-4.1-mini`, `text-embedding-3-small`), with the
version appended where Azure prices versions apart (`gpt-4o-2024-11-20`,
`gpt-chat-latest-2026-10-07`). Name the model by the deployment type you created, set the
deployment's `model` to your Azure deployment name (the default would send `datazone.gpt-6-luna`)
and its `region` to the Azure Region of the resource:

```yaml
providers:
  azure_openai: { credential: env:AZURE_OPENAI_API_KEY, api_version: "2025-04-01-preview" }
models:
  luna:
    catalog_id: azure_openai/datazone.gpt-6-luna   # a Data Zone Standard deployment
    deployments:
      - { provider: azure_openai, base_url: https://eu-north.example.openai.azure.com, model: luna-prod, region: swedencentral }
      - { provider: azure_openai, base_url: https://eu-west.example.openai.azure.com, model: luna-prod, region: westeurope }
```

- **Where Azure charges different prices in different Regions** (most Data Zone and Regional
  deployments), each price is its own entry scoped to the Regions that pay it (section 8), and there is
  no entry without `regions`. A deployment without a `region`, or in a Region Azure does not list for
  that model and deployment type, is refused with `model_not_priced` rather than charged another
  Region's price. The two Regions above share the EU Data Zone price, so they can share a route; a US
  deployment of the same model needs its own route (section 8, one route, one price).
- **Where the price is the same in every Region** (Global Standard, almost always) there is one entry
  without `regions`, which prices any Region.
- **The bare ids** (`azure_openai/gpt-6-luna` and the other hand-written ones) stay as they were:
  Global meters times the highest Data Zone uplift, a conservative upper bound for any deployment type.
- **Not priced:** Batch, Priority processing, Flex, provisioned (PTU), fine-tuned, Realtime, image and
  audio chat models, the 12-hour cache write, models Microsoft has retired or does not list on its
  models page, and `gpt-5.6-sol` while OpenAI's own price for it is promotional. A Region that lacks a meter the model has elsewhere (a cache-write or long-context price)
  is left out. The script prints each one when it runs. Use an override file (section 1) for them.
- **Updating** (maintainers): `uv run python scripts/azure_prices.py --download azure.json --check`
  exits 1 when the price list no longer matches the catalog; `--from-file azure.json --write` rewrites
  the generated block. The script stops on a meter name it cannot classify rather than guess.
  `tests/unit/test_azure_prices.py` and `tests/unit/test_azure_openai_catalog.py` test it on recorded
  API items.

## 11. Vertex AI

On Vertex AI (the `google` adapter with `auth: bearer_token`) Google prices the Gemini models
differently from the Gemini API, and differently on the global endpoint and on the others. The
bundled catalog carries Vertex prices under the same ids as the Gemini API (`google/gemini-4-neon`),
scoped to the Vertex Regions (section 8). A Vertex deployment always has a `region` (the
`locations/<region>` in its URL), so it takes the Vertex price; a Gemini API deployment has none and
keeps the Gemini API price. Set the Region you call:

```yaml
providers:
  google:
    base_url: "https://aiplatform.us.rep.googleapis.com"   # the us multi-region endpoint
    auth: bearer_token
    credential: adc
    region: us
models:
  gemini-neon: {catalog_id: google/gemini-4-neon, deployments: [{provider: google}]}
  gemini-neon-global:
    catalog_id: google/gemini-4-neon
    deployments: [{provider: google, base_url: "https://aiplatform.googleapis.com", region: global}]
```

- **Global and non-global are different prices.** The global endpoint (`region: global`,
  `https://aiplatform.googleapis.com`) has one entry; the multi-region (`us`, `eu`) and regional
  endpoints (`europe-west4`, ...) another, 10% dearer for Gemini 3 and later. One route cannot mix
  them (section 8), so route the global endpoint as its own model, as above.
- **Only the Regions Google sells a model in are listed.** Gemini 4 Neon is sold on `global`, `us` and
  `eu`, for example; Gemini 2.5 Flash in 23 regional endpoints and `global`. A Region the entry does
  not list falls back to the Gemini API price, and `check-config` warns about it.
- **List prices.** Where Google's pricing page shows a promotional price paid as credits back (Gemini 4
  Carbon and Argon until 2026-11-30, 3.8 Flash and 3.6 Flash until 2026-12-31), the catalog holds and
  charges the list price, so a budget runs out no later than the bill says.
- **Retired on Vertex.** Google's model pages give Vertex retirement dates that differ from the Gemini
  API's: Gemini 2.5 Pro, 2.5 Flash and 2.5 Flash-Lite were retired on Vertex on 2026-10-20, so a Vertex
  deployment of them is refused while the Gemini API still prices them.
- **Updating (maintainers):** the entries are generated, between the
  `# >>> generated by scripts/vertex_prices.py` markers in `prices.yaml`, from the Google Cloud Pricing
  API (the Vertex AI SKUs, exact prices from its v1beta price API) and Google's endpoint locations
  page. The download needs a Pricing API key (`GOOGLE_PRICING_API_KEY`, or a key file); the rest runs
  offline:

  ```bash
  uv run python scripts/vertex_prices.py --download /tmp/vertex-snapshot    # a new, empty directory
  uv run python scripts/vertex_prices.py --from-dir /tmp/vertex-snapshot --check   # exit 1 on drift
  uv run python scripts/vertex_prices.py --from-dir /tmp/vertex-snapshot --write
  uv run python scripts/vertex_prices.py --from-dir /tmp/vertex-snapshot --crosscheck  # Gemini API
  ```

  The script refuses (exit 2) a Gemini SKU whose description it cannot classify, a Gemini model the
  locations page adds, a zero or tiered price, and a model without reviewed limits in its `MODELS`
  table, so a new model is reviewed before it is priced. An unchanged entry keeps its dates. `--crosscheck`
  compares the Gemini API service's SKUs with the region-less entries and only reports.
  `tests/unit/test_vertex_prices.py` runs it on a recorded subset and checks that every non-global
  price is 1.1x the global one.

## 12. How prices stay current

Nothing in spendgate fetches a price at runtime. The bundled catalog changes only through a reviewed
pull request and a release ([ADR-0028](adr/0028-price-coverage-and-regional-prices.md) point 6):

- **Daily price watch.** [`.github/workflows/price-watch.yml`](../.github/workflows/price-watch.yml)
  runs every day on `main` (and by hand from the Actions tab). It downloads the AWS Price List, the
  Azure Retail Prices and the Google Cloud Pricing API SKUs, runs each importer's `--check`, and
  `--write` only where a generated block is out of date. When the catalog then differs from `main`'s,
  it force-pushes one bot branch, `prices/daily-watch`, rebuilt from `main` with one commit, and opens
  or updates the one pull request from it, titled `prices: daily price watch YYYY-MM-DD`. Its
  description lists, per importer, the ids added, changed or removed, the diff stat and the LiteLLM
  cross-check summary of the regenerated catalog. When nothing changed, the run is green and
  pushes nothing. It never merges.
- **A red run** means an importer refused its data (a meter it cannot classify, a new model without
  reviewed limits, a source format change) or a download failed, and nothing was proposed: read the
  failing step, then fix the importer or its reviewed table (sections 7, 10 and 11) in a normal pull
  request.
- **Reviewing its pull request.** Treat it like a contributor's price change: open each provider's
  page for the ids listed and compare the prices (a new id also needs its limits read from the model
  card). The diff is larger than the price change: a block that changed gets today's `checked_on` on
  every entry. Read the LiteLLM summary, and the full report in the run's `price-watch` artifact, as
  a second opinion, not a verdict (section 6). CI is started on the branch by the workflow, so the
  pull request shows `CI gate` on its head commit. Squash-merge it only after checking; a merged
  price change ships in a patch release ([releasing.md](releasing.md)). The next run rebuilds the
  branch from the new `main`, so leave edits to the branch to a separate pull request.
- **The Pricing API key** is the `GOOGLE_PRICING_API_KEY` secret of the `price-watch` environment
  (Settings → Environments), whose deployment branches are limited to `main`. Only the job that
  downloads the Vertex prices uses that environment, so a workflow changed on another branch cannot
  read the key, even when started by hand. The repository setting "Allow GitHub Actions to create
  and approve pull requests" must be on for the job to open its pull request; that does not bypass
  review, because the workflow is not a code owner.
- **By hand.** `make price-watch` does the same in a new temporary directory and prints the pull
  request text (it needs the network and the Pricing API key; `LITELLM_SHA=<commit>` adds the LiteLLM
  diff); review `git diff src/spendgate/catalog/prices.yaml` afterwards.
- **Weekly LiteLLM check.** [`.github/workflows/prices.yml`](../.github/workflows/prices.yml) still
  compares the whole catalog, hand-written entries included, with LiteLLM's latest commit every Monday
  and fails on a disagreement (section 6). The daily job only reports that diff.
