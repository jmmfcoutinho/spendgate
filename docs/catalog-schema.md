# Price catalog schema

Prices and model metadata are validated data, never code. Files: `catalog/prices.yaml` (bundled),
`catalog/schema.json` (here: [catalog-schema.json](../src/spendgate/catalog/schema.json), JSON Schema 2020-12), an
optional local override file. [catalog-example.yaml](catalog-example.yaml) is the example and holds every
price of a private tool that predates spendgate, except Realtime (on the roadmap). Consumers: `core/catalog.py`, `core/cost.py`.
`spendgate.config` reads the YAML files and computes their sha256; `core.catalog.parse(dicts, sha)` validates and
resolves them (core never reads files). No remote fetch at serve time: `spendgate catalog import` downloads
LiteLLM's price file only when an operator runs it, to draft an override file ([ADR-0014](adr/0014-catalog-import-command.md)), and
`spendgate catalog draft` writes a `verified: false` stand-in priced like another entry ([how-to section 9](catalog-how-to.md#9-when-a-model-has-no-price)).

```
uvx check-jsonschema --schemafile src/spendgate/catalog/schema.json docs/catalog-example.yaml   # from the repository root
```

## 1. Conventions

- **Decimal strings.** Every price and multiplier is a quoted string (`"0.012"`). YAML floats are
  rejected by the schema; the loader parses with `Decimal`.
- **Units in field names.** `_per_mtok` = per 1,000,000 tokens; `_per_minute` = per audio minute;
  `per_1k_characters`; `per_image`. All prices are in the entry's `currency`.
- **Money out.** Cost is computed exactly in the native currency, then converted once to integer
  nano-units of the ledger currency (`ledger.currency`) with ceiling rounding (section 5).
- **Provider part of `id`** is the slug = adapter module name (`openai`, `azure_openai`, `anthropic`, `bedrock`,
  `google`, `azure_speech`, `elevenlabs`); the model part is what the caller sends. There is no `vertex` slug:
  Vertex is an auth mode of `google`. Hyphenated slugs fail the schema.

## 2. Entry fields

| Group | Field | Type / values | Required | Meaning |
|---|---|---|---|---|
| Identity | `id` | `provider/model` | yes | Lookup key |
| | `mode` | chat, responses, messages, embedding, transcription, speech | yes | Endpoint family; decides which meters are mandatory |
| Price | `currency` | USD, EUR | yes | Currency of all meters |
| | `effective_from` | date | yes | First day this entry applies |
| | `source_url` | https URL | yes | Provider's official page |
| | `verified` | bool | yes | A person compared every price to `source_url` |
| | `checked_on`, `note` | date, text | no | Audit trail |
| | `deployment_type` | global, data_zone, regional, standard, batch, flex, priority, provisioned | no | Documentation of what the price is for |
| | `region` | slug | no | Documentation (for example `swedencentral`); never used for lookup |
| | `regions` | list of slugs, at least one, no repeats | no | The deployment Regions this entry prices, and only those (section 3, [ADR-0028](adr/0028-price-coverage-and-regional-prices.md)). Absent: every Region no entry in force lists, and deployments without a `region` |
| | `price_multiplier` | decimal > 0, default `"1"` | no | Applied to every meter, for example Data Zone over Global |
| Meters | `meters` | object, at least one | yes | See below; all members optional, subject to mode rules |
| Long context | `long_context` | `threshold_input_tokens`, `input_multiplier`, `output_multiplier` | no | Per model, never global |
| Limits | `limits.max_output_tokens` | int | **yes** for chat, responses, messages | Last link of the output-ceiling precedence (request, key, catalog; none means refuse, protocol 2.1); used by worst-case estimation. The Alpha/Terra/Sol seed uses the guard's `MAX_OUTPUT_TOKENS_LIMIT` (64000) as the documented ceiling, `verified: false` |
| | `limits.context_window` | int | no | Request validation |
| | `limits.max_image_tokens` | int | no | Input tokens the hold assumes for each image part: the most the model bills for one image at any detail level a client can request, plus a margin. Unset means the conservative default of 61,000 (the largest per-image cost a provider documents, 48,169, plus 25 percent). Set it only to lower the hold, citing the provider page and the date it was read in the entry's `note` or a comment above the entry (M-14). `tests/unit/test_catalog.py` keeps a per-provider minimum table that every bundled entry must meet |
| Lifecycle | `deprecation_date` | date | no | First day the model is refused (set it to the provider's shutdown date) |

### Meters

| Meter | Unit | Billed on usage field |
|---|---|---|
| `input_per_mtok` | per 1M tokens | `input_tokens` (text or other non-audio input) |
| `cached_input_per_mtok` | per 1M | `cached_input_tokens` (subset of `input_tokens`) |
| `cache_write_per_mtok` | per 1M | `cache_write_tokens` (subset of `input_tokens`) |
| `output_per_mtok` | per 1M | `output_tokens` |
| `reasoning_per_mtok` | per 1M | `reasoning_tokens` (subset of `output_tokens`) |
| `audio_input_per_mtok` | per 1M | `audio_input_tokens` (disjoint from `input_tokens`) |
| `cached_audio_input_per_mtok` | per 1M | `cached_audio_input_tokens` (disjoint) |
| `audio_output_per_mtok` | per 1M | `audio_output_tokens` (disjoint from `output_tokens`) |
| `audio_input_per_minute` | per audio minute | `audio_seconds` |
| `audio_granularity_seconds` | seconds, default 1 | rounding unit for `audio_seconds`, rounded UP, minimum one unit |
| `per_1k_characters` | per 1,000 characters | `characters` |
| `per_image` | per image | `images` |

Fallbacks when a meter is absent (conservative, never free): cached tokens bill at `input_per_mtok`;
cache-write tokens at `input_per_mtok`; reasoning tokens stay inside `output_tokens` at `output_per_mtok`.
Provider adapters normalise usage into this partition (text counters include their cached subsets;
audio counters are separate), so no token is billed twice. An unknown audio/text split is the adapter's
job to resolve to the dearer meter.

Mode rules (enforced by the schema): chat/responses/messages need `input_per_mtok` and `output_per_mtok`;
embedding needs `input_per_mtok`; transcription needs `audio_input_per_minute` or `audio_input_per_mtok`;
speech needs `per_1k_characters` or `audio_output_per_mtok`. A transcription model with both minute
and token meters (gpt-4o-transcribe) uses tokens when the reply reports usage and the minute rate for
the reservation and when usage is missing.

### `long_context`

If the request's `input_tokens` is **strictly greater** than `threshold_input_tokens`, the whole request
is priced at the higher rates: `input_multiplier` multiplies input, cached input, cache-write and audio-input
meters; `output_multiplier` multiplies output, reasoning and audio-output meters. Per-minute and
per-character meters are untouched. The earlier guard's rule (272,000; x2; x1.5) is written on each of its
three models, not in code. The hold takes the higher rates earlier: once the input estimate, which already
carries the 25% safety margin, reaches 80% of the threshold (`input_tokens x 1.25 >= threshold`; for text
alone, a raw count x 1.5625 >= threshold), or once the byte bound does: the UTF-8 bytes of the input text
(tool-call JSON at its widest rendering and opaque inputs such as signatures included), which no byte-level
tokenizer exceeds, plus the request, message, tool and structured-output overheads and the catalog image
ceiling (`limits.max_image_tokens`, else `DEFAULT_IMAGE_TOKENS`) per image. The bytes are a proof; the
overheads and the image ceiling are assumptions about hidden provider prompts and image billing. Settlement uses the reported count and the rule above.

### `settlement` (per provider, optional, top-level `providers:`)

Deliberately tiny. `free_statuses`: a subset of `[429, 529]` (throttling only); a reply with one of these statuses
means the provider rejected the call before processing it. Together with "the request was never sent" it defines the
core predicate `not_processed(attempt)` ([ledger-state-machine.md](./ledger-state-machine.md)): such an attempt costs 0 and
the request may fail over to the next deployment; anything else after send is charged per money rule M-13. Default `[429]`
for every provider when omitted (schema default); `anthropic` sets `[429, 529]`.
Any other status is a schema error. There is **no** zero-charge option for a reply without usage: such a call
settles as `ok_no_usage` and charges the full reservation. Catalog data cannot fail open; every fail-open
behaviour lives in `spendgate.yaml` under `fail_open:` and is logged at start (ADR-0003). Everything else is core
behaviour, not data.

## 3. Resolution rules

A lookup takes the catalog id and the deployment's Region (its `region`, or its provider's; none if
neither is set).

1. Candidates: entries with the requested `id` and `effective_from <= today` (UTC date of the call).
2. Region tier: if any candidate lists the deployment's Region in `regions`, only those candidates count.
   Otherwise only the candidates without `regions` count. A deployment without a Region matches only
   entries without `regions`; a Region-scoped entry is never used for a Region it does not list.
3. The **newest** `effective_from` among the counted candidates wins. Older entries stay for history and replay.
4. If the winner has `deprecation_date <= today`, the model is **refused** (no fallback to an older entry).
5. No candidate, or an expired winner: error code **`model_not_priced`**, no upstream call.
6. Two entries with the same `id`, `effective_from` and `regions` in one file are a load error. Entries
   of one `id` and `effective_from` must not list the same Region (in one file or across files), and at most
   one of them may omit `regions`.
7. Override file: same schema; its entries are merged by `(id, effective_from, regions)` (the set of Regions,
   in any order), an override replaces the bundled one. Its `providers` keys replace the bundled block for that
   provider. To retire a bundled model locally, add an entry with a newer `effective_from` and a past
   `deprecation_date`.
8. One route, one price: every deployment of a `models:` route must resolve to the same entry, today and on
   every later `effective_from` already in the catalog. `check-config` and `serve` refuse a route whose
   deployments resolve to different entries (a configuration error, exit 3), naming the route and the Regions;
   the request path refuses it too (`model_not_priced`). So the hold and the charge use one entry per request.
9. The loader validates against the schema at startup and refuses to start on any error. It records
   the catalog sha256 (`catalog_sha`) and the chosen entry (`price_id`) on every `calls` row (T-CAT-05):
   `<id>@<effective_from>`, plus `~<region>` (the serving deployment's Region) when a Region-scoped entry
   priced the call, for example `azure_openai/gpt-x@2026-11-01~swedencentral`. Region sets of one id and date
   are disjoint, so the suffix names exactly one entry.

Two entries for one id (illustrative numbers):

```yaml
- {id: openai/example, effective_from: "2026-01-01", meters: {input_per_mtok: "2", output_per_mtok: "8"}, ...}
- {id: openai/example, effective_from: "2026-09-01", meters: {input_per_mtok: "1", output_per_mtok: "4"}, ...}
```

## 4. Mapping: earlier guard constant to catalog field

| Earlier guard (file / constant) | Catalog |
|---|---|
| `pricing.RATES[m].input` (USD per 1M) | `meters.input_per_mtok` x `price_multiplier` |
| `RATES[m].cached` | `meters.cached_input_per_mtok` |
| `RATES[m].output` | `meters.output_per_mtok` |
| Data Zone x 1.20 (comment) | `deployment_type: data_zone`, `price_multiplier: "1.20"`; stored meters are the Global prices |
| `LONG_CONTEXT_THRESHOLD = 272_000` | `long_context.threshold_input_tokens` |
| `_LONG_INPUT_FACTOR = 2` (input and cached) | `long_context.input_multiplier` |
| `_LONG_OUTPUT_FACTOR = 1.5` | `long_context.output_multiplier` |
| `AUDIO_RATES[m].usd_per_minute` | `meters.audio_input_per_minute` |
| `AUDIO_RATES[m].tokens` (audio in, text in, text out) | `audio_input_per_mtok`, `input_per_mtok`, `output_per_mtok` |
| `billed_seconds` (whole seconds, round up, min 1) | `audio_granularity_seconds` (default 1) |
| `RealtimeRates.*` | not in v0.1 (Realtime is on the roadmap); the meters stay in the schema for it |
| `ELEVEN_RATES[m]` (USD per 1,000 chars) | `meters.per_1k_characters` |
| `model-prices.yaml` `input_per_million` etc. | same meters; the `<model>:audio` split rows merge into one entry |
| `model-prices.yaml` `per_minute` | `audio_input_per_minute` |
| `model-prices.yaml` `source`, `checked_at`, `estimated` | `source_url` + `note`, `checked_on`, `verified` |
| `USD` implicit everywhere; `usd_to_eur` argument | `currency` per entry; FX rate from the `spendgate.yaml` `fx` block (`rates`, `as_of`, `source`), stored per call as `fx_rate_nano`; refuse to start when a catalog currency differs from the ledger currency and no rate is configured (library quick start: the ledger currency follows the cap's currency, so a USD cap needs no rate) |
| `RATES[model]` KeyError for unknown models | `model_not_priced` |
| `_to_nano` ROUND_CEILING | unchanged rule, section 5 |

Not prices, so not in the catalog (they belong to estimation): `REALTIME_SESSION_TOKENS` and
`realtime_output_cap` (Realtime), the 1-token-per-2-characters text estimate, `TTS_MAX_INPUT_CHARS`,
`billed_characters` (max of text length and the provider's reported cost).

Every price of the earlier guard is expressible; the one conceptual change is the Azure OpenAI deployment-zone models, which move from
"final rate" to "Global rate x multiplier" (same product, exact in Decimal). Seed provenance: entries with a
URL in the earlier guard's code (ElevenLabs) are `verified: true`; every other price keeps the earlier guard's
number with the provider's official page and `verified: false` until it is re-checked.

## 5. Cost formula and worked examples

```
native   = sum(units_i * rate_i) * (long_context factor per meter) * price_multiplier
usage_cost_nano = ceil( native * fx_to_ledger * 1_000_000_000 )      # one ceiling, at the end
```

`fx_to_ledger` is 1 when `currency` equals the ledger currency; otherwise the rate from the config `fx` block
(`fx: {rates: {USD: "0.92"}, as_of, source}`), stored on the `calls` row as `fx_rate_nano`. A missing rate is a startup error (T-MON-07). Never round per meter.

**Example 1: `azure_openai/gpt-6-luna`** (USD, Data Zone x 1.20), 10,000 input tokens of which 4,000 cached,
2,000 output, FX 0.9137 EUR per USD. Effective rates per 1M: input 0.10 x 1.20 = 0.12, cached 0.012,
output 0.60. Input 10,000 is below 272,000, so no long-context factor.

- uncached 6,000 x 0.12 = 720; cached 4,000 x 0.012 = 48; output 2,000 x 0.60 = 1,200; sum 1,968 (USD x 1e-6)
- native = 0.001968 USD
- EUR = 0.001968 x 0.9137 = 0.0017981616; nano = 1,798,161.6
- **ceiling -> 1,798,162 nano-EUR**

**Example 2: `azure_openai/whisper`** (USD, `audio_input_per_minute` 0.00792), a 95.3 s clip, same FX.
Granularity 1 s: 95.3 s rounds up to 96 s = 1.6 min.

- native = 1.6 x 0.00792 = 0.012672 USD
- EUR = 0.012672 x 0.9137 = 0.01157840640; nano = 11,578,406.4
- **ceiling -> 11,578,407 nano-EUR**

Long-context check (same entry, 300,000 input, none cached, 1,000 output): input rate 0.12 x 2 = 0.24,
output rate 0.60 x 1.5 = 0.90; native = (300,000 x 0.24 + 1,000 x 0.90) / 1e6 = 72,900 / 1e6 = 0.0729 USD.

Worst case (`worst_case`) uses the same formula with every estimated input token uncached at
the dearest applicable input meter, the output ceiling at the dearest output meter (output, reasoning or
audio output), long-context applied once the margined input estimate reaches 80% of the threshold
(`input_tokens x 1.25 >= threshold`, `LONG_CONTEXT_MARGIN_PERCENT`) or the estimate's byte bound
(`input_tokens_bound`: one token per UTF-8 byte of the text, plus the assumed overheads and image ceiling)
reaches it (the estimate is counted without the provider's tokenizer, so the real count may be above it; the
bytes of the text cannot be: M-14), and the dearer of minute vs
token meters for audio.

## 6. Open points

- `deprecation_date` doubles as the refusal date; providers announce deprecation before shutdown, so the
  catalog should hold the shutdown date. A separate `warn_from` is not proposed for v0.1.
- `cached_audio_input_per_mtok` is kept for Realtime and Gemini audio; `audio_granularity_seconds`
  extends the meter list for the earlier guard's whole-second rounding.
- Batch/flex/priority prices: one entry per variant, each with its own `id`; how an adapter selects a
  variant (an id suffix is the likely answer) is left to the implementation.
