# How to add a model to the catalog

Prices are data. A new model, or a new price for an existing one, is a YAML edit; no code changes.
The format is described in [catalog-schema.md](catalog-schema.md) and enforced by
[`src/spendgate/catalog/schema.json`](../src/spendgate/catalog/schema.json). This page is the
short path. A model that has no price in force is refused with `model_not_priced`, so a missing
entry is safe, never free.

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
  refuses to guess the largest possible bill.
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

## 3. Load it

Put the entry in a file of your own and list it in the config:

```yaml
catalog:
  paths: [./my-prices.yaml]
```

An override is applied after the bundled catalog and replaces an entry with the same `id` and
`effective_from`; anything else is added. Then route the model in `models:`:

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
dated, sourced file, and the tooling below is for maintainers and operators at their desk.

[`scripts/litellm_prices.py`](../scripts/litellm_prices.py) reads LiteLLM's
`model_prices_and_context_window_backup.json` from one pinned commit of `BerriAI/litellm` (the
default is hard-coded in the script; `--commit <sha>` picks another, `--file <path>` reads a local
copy). It records the SHA-256 of the bytes it read and never writes into `prices.yaml`.

```bash
# Compare our catalog with LiteLLM: equal, different (both values, % and direction), ours only, theirs only.
uv run python scripts/litellm_prices.py diff
uv run python scripts/litellm_prices.py diff --commit <sha> --threshold 0.5 --out report.txt

# Draft entries for models we do not carry yet (prints YAML; --out writes a file).
uv run python scripts/litellm_prices.py import gpt-6-luna gemini/gemini-4-carbon --out draft.yaml

# Breadth for an operator: a full override file of every mappable LiteLLM model we do not carry.
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
report as an artifact.

**The `override` file** is for operators who prefer breadth over audit: every entry is
`verified: false`, so the start-up warning in section 2 lists them. The risk is yours: a stale or
tampered number there is a wrong charge in your ledger. Load it like any override
(`catalog.paths`, section 3) only after reading the diff, and prefer a handful of checked entries.
