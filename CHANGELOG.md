# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Release images are published for `linux/arm64` as well as `linux/amd64`, so they run on Apple Silicon
  and other arm64 hosts without emulation (#123).

## [0.5.0] - 2026-11-27

A security release. Upgrade every gateway: four advisories are fixed (below), and budget-warning
webhooks are delivered again.

**Upgrading from 0.4.0:** no migration is needed, but four things can change what a running gateway
does.

- **Every key needs a real budget on its scope chain.** A session's own budget no longer counts as
  one. If a key's only limit was `session_defaults` `budget_nano`, every call on it, with or without
  `X-Spendgate-Session`, is now refused `forbidden` (`no_budget`). Before upgrading, add a budget on
  the key's scope or an ancestor (`spendgate budgets set <scope> --cap ...`); session budgets keep
  narrowing it (ADR-0030).
- **Session budgets need `budgets:write`.** An admin key with `keys:write` but not `budgets:write`
  can no longer create keys with a session budget or a looser new-session bound, nor raise them;
  lowering still needs only `keys:write`. Rotating a killed key now yields a killed key: lift the
  switch first if you meant to hand out a working replacement (ADR-0030).
- **Some audio uploads are refused.** Transcription files are measured by counting their audio
  (ADR-0029). MP4 audio other than AAC, WAV other than PCM, float, A-law or mu-law, WebM/Matroska
  audio other than Opus or Vorbis, and files whose headers disagree with their contents are refused
  `invalid_request` (`param` `file`) instead of measured.
- **Token-billed transcription entries need an output ceiling.** A custom catalog entry with `mode:
  transcription` and an output meter (`output_per_mtok`, `reasoning_per_mtok` or
  `audio_output_per_mtok`) needs `limits.max_output_tokens`, or the catalog does not load. Holds for
  these models grow by that limit at the output price (ADR-0031).

### Security

- Transcription holds could be far below the provider's bill (GHSA-746r-67rq-v763). For FLAC,
  MP4/M4A, Ogg, WAV and WebM uploads the duration was read from fields the file declares about
  itself, which the caller controls, while the provider bills the audio it decodes. A file whose
  header understated its length was held at a small fraction of its cost, so calls could be admitted
  on budgets that could not pay for them; the full charge was booked afterwards as overrun. The
  duration is now counted from the audio itself (packets, frames, sample tables, PCM bytes), a
  declared length can only raise it, and a size check refuses files holding more audio than their
  counted length can carry (ADR-0029, money rule M-16). Affects every release since 0.1.0. Until
  upgraded, do not allow transcription models on keys used by untrusted callers.
- A session budget no longer counts as a budget (GHSA-wwcc-q273-7g4j). Admission admitted a call
  when the only budget on its key's scope chain was the session's own budget, which comes from the
  key's `session_defaults` and could be set with `keys:write` alone. An admin key with only
  `keys:write` could therefore create a scope with no budget and a key whose sessions had a large
  budget, and spend without limit by naming new sessions. A call is now admitted only when a budget
  on a non-session scope of the key's chain applies; otherwise it is refused `forbidden` with
  `details.reason` `no_budget`. Setting or raising `session_defaults.budget_nano`, or raising
  `max_new_sessions_per_hour` or setting it to `null`, now also needs `budgets:write` (M-17,
  ADR-0030).
- Rotating a key no longer lifts its key kill switch (GHSA-wwcc-q273-7g4j). A `key` kill switch now
  applies to the key and to every key that replaces it by rotation, open streams included; lifting it
  lifts it for all of them. A key switch stops a credential, not a scope: a `keys:write` holder can
  still create a new key on the same scope, which a `scope` switch stops (M-18, ADR-0030).
- Token-billed transcription (Azure OpenAI gpt-4o-transcribe, gpt-4o-mini-transcribe,
  gpt-4o-transcribe-diarize) now holds its transcript (GHSA-hcm8-vrj6-w6jx). The hold covered only
  the audio and the prompt, so every call could settle above it by up to the model's output limit.
  The hold now takes the entry's `limits.max_output_tokens` in full, whatever the key sets, because
  no transcription request field bounds the transcript; the bundled entries carry OpenAI's 2,000
  tokens (M-19, ADR-0031).
- `max_new_sessions_per_hour` is enforced under concurrency on PostgreSQL (GHSA-773v-hmv2-87vp).
  Concurrent calls opening new sessions on one key could each pass the hourly count and create more
  sessions than allowed. New-session admission now locks the key row before counting. SQLite was not
  affected (M-20, T-SESS-10).
- `spendgate serve` writes no HTTP access log. uvicorn's access log recorded each request target with
  its query string, where some clients put a key; each call's own JSON line replaces it, and
  uvicorn's start, stop and error lines stay on stderr (T-LOG-05).
- Container image: glibc (`libc6`, `libc-bin`) is upgraded to Debian's fix for CVE-2026-18374,
  CVE-2026-19499, CVE-2026-19542 and CVE-2026-5435, which the pinned `python:3.13-slim` digest
  predates.
- The daily price watch reads the Google Pricing API key from the `price-watch` environment, which only
  `main` may use, instead of a repository secret: a workflow changed on another branch can no longer
  read it, even when started by hand (#109).

### Fixed

- Budget-warning webhooks are delivered again. Since 0.2.0, `spendgate serve` counted every warning
  as `spendgate_alert_webhooks_total{result="dropped"}` and posted none: uvicorn reconfigured logging
  at start, which closed the webhook handler, and the handler took that as its own "stopped" flag.
  The flag has its own name, and `serve` no longer lets uvicorn reconfigure logging. If you alert on
  `dropped`, as `docs/operations.md` recommends, you saw this; the ledger and `spendgate status` were
  always right.

## [0.4.0] - 2026-11-27

**Upgrading from 0.3.1:** no migration is needed, but three things can change what a running gateway does.

- **One route, one price.** A route whose deployments' Regions resolve to different catalog entries no
  longer starts (exit 3, naming the route and the Regions; see Changed). Split such a route into
  separate models. With the bundled catalog this can affect: Vertex AI routes that mix the `global`
  endpoint with regional or multi-region ones; and Bedrock routes that span Regions AWS prices
  differently. The new Azure OpenAI entries use new ids (`azure_openai/global.`, `datazone.` and
  `regional.<model>`), so no existing Azure route is affected.
- **Region-scoped prices need `region`.** A deployment is priced by a Region-scoped entry only when its
  `region` is set (Vertex deployments always have one; set it on Bedrock and Azure deployments). Of
  the Bedrock ids bundled before, only `global.anthropic.claude-sonnet-5-5` became Region-scoped: AWS
  lists no global price for it in ap-northeast-1 and ap-northeast-3, so it is refused there. Every
  other bundled price is unchanged.
- **Vertex is priced as Vertex.** A Vertex deployment is now charged Google's Vertex list price for its
  Region instead of the Gemini API price, and Vertex refuses Gemini 2.5 Pro, 2.5 Flash and 2.5
  Flash-Lite, which Google retired on Vertex on 2026-10-20.

### Added

- Bedrock prices for every text model the Converse adapter can serve, generated by a new maintainer
  importer, `scripts/bedrock_prices.py`, from the AWS Price List bulk API (offers
  `AmazonBedrockFoundationModels` and `AmazonBedrock`, all Regions) and a reviewed table of what the
  price list does not say (`scripts/bedrock_models.yaml`: model ids, inference profiles and their
  source Regions, limits and retirement dates, from AWS's model cards). 84 models and 171 catalog ids:
  Claude 4.x and 5.x, Amazon Nova, Meta Llama 3 and 4, Mistral, DeepSeek, Qwen, OpenAI GPT-5.x and
  GPT-6.x and gpt-oss, Gemma 3, MiniMax, Moonshot Kimi, NVIDIA Nemotron, Writer Palmyra and Z.ai GLM,
  each under every profile AWS offers it in (`us.`, `eu.`, `apac.`, `jp.`, `au.`, `ca.`, `in.`,
  `us-gov.`, `global.`) and in-Region. Where the price depends on the Region the call is made from,
  each price is its own Region-scoped entry (ADR-0028). `--check` exits 1 when the bundled block no
  longer matches the Price List, for the daily price job (#109); `docs/catalog-how-to.md` section 7
  lists the models and how to run it. Models left out (no Converse, no documented output limit,
  bedrock-mantle prices only, retired) are listed with their reason in the table. Before this, every
  Bedrock model but the current Claude ones was refused as `model_not_priced` (#105).
- `scripts/litellm_prices.py diff` compares an id with Region-scoped entries by the Region price that
  agrees with LiteLLM, else the cheapest (so a price of ours below LiteLLM's still fails), and its
  allow-list accepts a LiteLLM long-context rate the provider does not bill (`long_contexts`,
  `not_offered`).
- Prices for every current model the `openai`, `anthropic` and `google` (Gemini API) adapters serve,
  read from each provider's own pricing, model and deprecation pages on 2026-11-26 and cross-checked
  with LiteLLM (no price differs). Anthropic: 16 more Claude models, from Opus and Sonnet 5.5 to
  Haiku 4.5, including the Mythos models for verified organisations. OpenAI: 28 more chat and
  Responses models (GPT-6.2 Astra and Sol down to GPT-4o mini and o3), `text-embedding-3-large`,
  `text-embedding-ada-002`, and `tts-1` and `tts-1-hd`, the first OpenAI speech models with a price.
  Gemini API: 13 more text models, among them Gemini 4 Argon, Neon and Hydrogen and Gemini 3.8 Flash,
  with their announced price changes (Argon doubles on 2026-12-01, 3.8 Flash and 3.6 Flash on
  2027-01-01) as dated entries. Models the providers have scheduled for shutdown carry that date and
  are refused from it (Gemini 2.5 Pro and 2.5 Flash on 2026-12-01, for example). Each catalog comment
  lists the models left out and why, such as `gpt-5.6-sol`, whose promotional price ends on 2026-11-30
  with no later price published. Before this, these models were refused as `model_not_priced` (#108).
- Region-scoped prices: a catalog entry may list the deployment Regions it prices (`regions: [...]`), and
  prices only those. The deployment's `region` picks the entry; every other Region, and a deployment
  without a `region`, takes the region-less entry. Entries of one id and date must not list the same
  Region. The call row's `price_id` gains `~<region>` when a Region-scoped entry priced the call
  (`azure_openai/gpt-x@2026-11-01~swedencentral`); the ledger schema is unchanged. `check-config`,
  `serve` and library mode warn about a deployment Region that no entry of its Region-scoped id
  lists, naming the Regions the catalog does list, so a misspelt Region is visible at start.
  `docs/catalog-how-to.md` section 8 shows how to write one (ADR-0028, #103).
- Vertex AI prices for the 20 Gemini models the `google` adapter serves on Vertex (`auth:
  bearer_token`), in every Region Google sells each one: 34 Region-scoped entries with the same ids as
  the Gemini API (`google/gemini-4-neon` and so on), so a Vertex deployment (which always has a
  `region`) takes the Vertex price and the Gemini API keeps its own. The global endpoint (`global`) and
  the non-global endpoints (`us`, `eu` and the regional ones, 10% dearer for Gemini 3 and later) have
  separate entries. Gemini 3.5 Flash, 3.7 Flash and the Cyber variants of Gemini 4 and 3.8 Flash are
  priced on Vertex only. Prices are Google's list prices from the Cloud Pricing API: where Google gives
  a promotional price as credits back (Gemini 4 Carbon and Argon, 3.8 Flash, 3.6 Flash, Robotics-ER 2),
  the hold and the charge use the list price. Vertex retirement dates from Google's model pages are
  `deprecation_date`s (2.5 models on 2026-10-20, 3.6 and 3.7 Flash on 2027-01-28). The maintainer
  importer `scripts/vertex_prices.py` generates the block from the Pricing API and Google's endpoint
  locations page, refuses any Gemini SKU it cannot classify, and `--check` reports drift for the
  daily price-watch job. `docs/catalog-how-to.md` section 11 shows a Vertex config (#107).
- `spendgate catalog draft --like <catalog id> --as <catalog id> [--multiplier <m>] [--regions <slug,...>]
  [--region <slug>] [--config <file>] --out <file> [--force]` prices a model the catalog lacks like one
  it has, of the same provider: it copies the entry in force today (and any scheduled price after it),
  multiplies every meter by `m` (a decimal of at least 1, default 1; exact, never rounded) and writes an
  override entry marked `verified: false` whose note names the copied entry. `--regions` scopes the
  stand-in to those Regions (`regions:`); `--region` picks which price of a Region-scoped `--like` is
  copied. Like `catalog import`, it never overwrites `--out` without `--force` and loads the result
  before writing. `docs/catalog-how-to.md` section 9 (ADR-0028, #104).
- Azure OpenAI prices for every deployment type and Region Azure sells them in, generated from the
  public Azure Retail Prices API by a new maintainer script, `scripts/azure_prices.py` (#106). 46
  models the `azure_openai` adapter serves (GPT-6.2 down to GPT-4o mini, `gpt-chat-latest`,
  `gpt-oss-120b`, three embedding models, four transcription models and `tts`/`tts-hd`) get 103 new ids,
  one per deployment type: `azure_openai/global.<model>`, `azure_openai/datazone.<model>` and
  `azure_openai/regional.<model>`, in 156 entries over 42 Regions. Where Azure charges different prices
  in different Regions (89 entries, most Data Zone and Regional deployments), each price is scoped to its
  Regions and a Region Azure does not list is refused rather than charged another Region's price.
  Limits come from Microsoft's models page and shutdown dates from its retirement schedule. Batch,
  Priority, Flex, provisioned, fine-tuned, Realtime, image and audio chat meters, the 12-hour cache write
  and retired models are not imported, nor is `gpt-5.6-sol`, left out like `openai/gpt-5.6-sol` while
  OpenAI's price is promotional. The script refuses any Azure OpenAI meter name it cannot
  classify, and `--check` (exit 1 on drift) is ready for the daily price-watch job. The hand-written
  bare ids (`azure_openai/gpt-6-luna` and the others) are unchanged. See "Azure OpenAI: which catalog
  id" in docs/catalog-how-to.md.
- A daily price watch for maintainers (ADR-0028 point 6): `.github/workflows/price-watch.yml` runs the
  Bedrock, Azure OpenAI and Vertex AI importers on fresh provider data every day and, when their
  generated catalog blocks change, opens or updates one pull request (`prices/daily-watch`) listing the
  ids each importer added, changed or removed, with the LiteLLM diff summary. It never merges; an
  importer that refuses its data fails the run and nothing is proposed. The proxy still fetches no
  prices at runtime. `scripts/price_watch.py` and `make price-watch` do the same by hand. See "How
  prices stay current" in docs/catalog-how-to.md.

### Changed

- One route, one price: `check-config` and `serve` refuse (exit 3) a route whose deployments resolve to
  different catalog entries, today or from a later `effective_from`, naming the route and the Regions.
  Until now the hold and the charge silently used one price for every deployment of a route; route
  differently-priced Regions as separate models (ADR-0028, #103).
- Gemini API: Gemini 4 Carbon and Gemini 4 Argon are charged their billed list price ($4 input,
  $0.20 cached, $20 output per MTok) from 2026-11-26, not the $2 / $0.10 / $10 the pricing page shows
  through 2026-11-30. The Gemini API billing SKUs (Cloud Billing service `AEFD-7695-64FA`) list the
  full price and the promotion is paid as credits back, so the lower price would undercharge. The
  2026-12-01 entries already had this price (#107).
- Catalog overrides now merge by `id`, `effective_from` and `regions`: an override with a different set of
  `regions` adds an entry instead of replacing the region-less one, and one whose Regions overlap another
  entry of the same id and date is a load error.
- The `model_not_priced` refusal and the `check-config` error for an unpriced route now say how to
  price the model: an override file in `catalog.paths`, or a stand-in from `spendgate catalog draft`.
  The code, status and the leading `<provider>/<model>: <reason>` of the message are unchanged (#104).

### Fixed

- Azure OpenAI `gpt-4o-transcribe` was refused from 2026-10-15; Microsoft's retirement schedule (last
  updated 2026-11-18) retires it on 2027-02-26, so it is priced until then. The retirement dates
  Microsoft now confirms are set on the bundled Azure entries (gpt-6-astra, gpt-6-luna, gpt-6.1-sol,
  gpt-6.1-terra, gpt-transcribe, gpt-live-transcribe) (#106).

## [0.3.1] - 2026-11-26

**Upgrading from 0.3.0:** no migration and no config change is needed. On Bedrock you can now name the
inference profile you call as the catalog id (`bedrock/eu.anthropic.claude-haiku-5-5`, `bedrock/global.…`)
instead of mapping the bare id with `model:`; `docs/catalog-how-to.md` section 7 lists them.

### Added

- Bedrock prices for every geography AWS offers the current Claude models in: an entry per inference
  profile for Haiku 5.5 (`us.`, `ca.`, `eu.`, `au.`, `jp.`, `global.`), Sonnet 5.5 (`us.`, `ca.`, `eu.`,
  `au.`, `global.`), Opus 5.5 (`us.`, `eu.`, `au.`, `jp.`, `global.` and in-Region in eu-west-2) and
  Fable 5.2 (`us.`), from the AWS Price List read for all 35 Regions. Sonnet 5.5 and Opus 5.5 on Bedrock,
  and any profile outside the US, were refused as `model_not_priced` before. `docs/catalog-how-to.md`
  says which id to use per geography (#100).

- Live smoke test of AWS Bedrock (eu-west-1, Claude Haiku, Sonnet and Opus 5.5 through `eu.` inference
  profiles), recorded in `docs/reviews/live-smoke-v0.2.md`: plain, streamed and tool calls all pass and
  the Haiku charges match the catalog to the nano-unit. The Bedrock contract tests now also run on
  answers recorded from the live API, including the raw event-stream bytes (#9).

### Fixed

- Bedrock: a provider 401, 403 or 404 (`AccessDeniedException`, `UnrecognizedClientException`,
  `ResourceNotFoundException`: the gateway's own AWS credentials, model access or model id) is
  `upstream_unavailable` (502, provider named), as protocol.md 4.2 rule 1 says and every other
  adapter does. It was a 500 `internal_error` ("The provider rejected the gateway."), which reads as
  a gateway bug and which SDKs retry. The charge (0, nothing processed) is unchanged.

## [0.3.0] - 2026-11-25

**Upgrading from 0.2.x:** no migration and no config change is needed. Pull the 0.3.0 images: the
0.2.0 images carry a vulnerable `libzstd1` (see Security). A `server.key_pepper` shorter than 16
bytes, which 0.2.0 already refused with a traceback, is now reported by `check-config` too.

### Added

- `docs/reviews/live-smoke-v0.2.md`: a live smoke test of OpenAI chat, Responses and embeddings,
  Anthropic, and Google (Gemini API and Vertex AI) through the gateway, every charge matching the
  catalog to the nano-unit. The contract tests of those adapters now also run on answers recorded
  from the live APIs, ids and signatures replaced with fakes (#9). AWS Bedrock is still to run.
- `deploy/compose/compose.admin.yaml`: an optional overlay that puts the admin API on an internal
  `admin` network which only a new `operator` service joins, so an operator container or a
  `/metrics` scraper can reach the admin port while the apps on `apps` cannot, by name or by
  address. Set `server.admin.listen: spendgate-admin:8081` with it; the default (loopback inside
  the guard's container) is unchanged. A perimeter test checks both sides
  ([ADR-0027](docs/adr/0027-admin-network.md), #11).
- ElevenLabs speech accepts `language_code` (ISO 639-1, two lowercase letters such as `pt`) on
  `POST /v1/audio/speech` and forwards it, so non-English read-aloud is spoken in the right
  language (#10). It takes effect only on models that support it, such as the v2.5 Turbo and
  Flash models; ElevenLabs ignores it on others, including `eleven_multilingual_v2`. An invalid
  value is `invalid_request` with `param: language_code`; OpenAI and Azure OpenAI speech still
  refuse the field ([ADR-0026](docs/adr/0026-elevenlabs-language-code.md)).

### Fixed

- Google: a provider 401, 402, 403 or 404 is `upstream_unavailable` (502, our key, billing or model
  config), as protocol.md 4.2 rule 1 says. A Gemini API account out of prepaid credit (402
  `RESOURCE_EXHAUSTED`) was reported to the client as `invalid_request` (400). The charge (0, nothing
  processed) is unchanged.
- A `server.key_pepper` shorter than 16 bytes is a configuration error naming the setting
  ("server.key_pepper: must be at least 16 bytes", exit code 3) for `check-config`, `serve`,
  `migrate` and every `--local` command. `check-config` used to report "configuration ok" and the
  others ended in a `ValueError` traceback (exit 1); nothing started either way. The pepper's
  value is never shown, and the key store still refuses a short pepper on its own.

### Security

- Every test id the threat model cites now has a test (review finding SR-10): new tests for
  attribution headers (T-ATTR-01..03), the overrun metric (T-CAT-04), a clock jumping back and
  forward across midnight (T-CLK-01), holds of a crashed instance (T-HOLD-02), a flood of holds
  from one key (T-HOLD-03), key material, prompts and answers kept out of the ledger and error
  bodies (T-LOG-02, T-LOG-03), metric labels (T-OBS-02) and provider redirects (T-SSRF-02), plus
  Docker tests for the admin port (T-ADM-03) and the container's user, read-only root and
  capabilities (T-CTR-01, T-CTR-02). `tests/unit/test_threat_model.py` also fails when the
  threat model names a test file that does not exist.
- A flood of distinct well-formed fake keys still costs one ledger read each, with no per-peer
  throttle; this is now recorded as accepted risk R-06 (threat TH-20, finding SR-11), and a test
  (T-AUTH-06) pins that such a key costs that read and nothing else. The owner-versus-sweeper
  race test (CR-10) is recorded as closed: the two ordered tests force each order.
- Container image: `libzstd1` is upgraded to Debian's fix for CVE-2026-124851 (zstd information
  disclosure), which the pinned `python:3.13-slim` digest predates. The 0.2.0 images carry the
  vulnerable package; the 0.3.0 images ship the fix.

## [0.2.0] - 2026-11-25

**Upgrading from 0.1.x:**

- **Postgres: run `spendgate migrate` before starting 0.2.0** (or start it with `serve --migrate`,
  or set `ledger.auto_migrate: true`). `serve` no longer migrates a Postgres ledger at start and
  refuses with exit code 5 while the schema is behind
  ([ADR-0018](docs/adr/0018-migration-policy.md)). SQLite still migrates at start.
- Bounded defaults for agents ([ADR-0013](docs/adr/0013-safer-agent-defaults.md)): a key without
  its own `concurrency` admits 16 calls at once (`defaults.limits.concurrency`, also in library
  mode), and session defaults allow 60 new sessions per hour unless they set
  `max_new_sessions_per_hour`. An explicit `null` keeps either unlimited.
- New `calls.error_code` rows hold stable codes such as `read_timeout` instead of exception class
  names ([ADR-0022](docs/adr/0022-stable-error-codes.md)); queries matching the old names should
  also match the new ones.

### Added

- `docs/releasing.md`: how a release and a security release ship (tagging, the environment
  approvals, the advisory's private fork and its bypass merge), and how to merge a stack of pull
  requests.
- `spendgate catalog import --from-litellm <commit-sha> --out extra.yaml [--providers ...]`
  drafts a catalog override file from LiteLLM's `model_prices_and_context_window.json` at one
  pinned commit (https to raw.githubusercontent.com only, no redirects), or from a local copy
  with `--from-file`. Every entry is `verified: false`; models the installed bundled catalog
  carries are left out when the file is drafted. Entries with a non-finite or non-numeric
  value, a zero price, a long-context multiplier below 1, or no whole output ceiling are skipped
  one by one with a reason. An existing file is kept unless `--force`, which replaces it
  atomically. Nothing is fetched at serve time (ADR-0014). The mapping now lives in
  `spendgate.core.litellm` and `scripts/litellm_prices.py` uses it.
- `spendgate check-config` warns when a `verified: false` catalog entry supersedes a verified
  entry of the same id, for example a drafted entry for a model a later release bundles. Re-run
  the import or delete such entries after upgrading.
- `serve --migrate` and the config key `ledger.auto_migrate`, and expand/contract migrations: a
  migration marked `-- compatible_with_previous: true` is recorded as such
  (`schema_migrations.compatible`), and code starts on a schema exactly one such step ahead, so
  the previous release can run after an upgrade's migration. Postgres commits each migration on
  its own. A lint test checks new migrations (a Postgres `CHECK` needs `NOT VALID` and a later
  `VALIDATE`; a compatible migration may not be destructive or add a constraint). Upgrade and rollback runbook in
  [operations](docs/operations.md#upgrades-and-rollbacks); see
  [ADR-0018](docs/adr/0018-migration-policy.md).
- Cloud identity (#33). `credential: adc` for Vertex AI (`google` with `auth: bearer_token`)
  uses Google application default credentials (workload identity, the metadata server) instead
  of a service-account key. `ledger.backend.auth: {kind: rds_iam | entra | cloudsql_iam}` logs
  every new Postgres connection in with a short-lived RDS IAM, Entra ID or Cloud SQL IAM token
  instead of a password; the DSN then names the user and carries no password. Each kind uses the
  extra of its cloud (`bedrock`, `azure`, `google`): no new dependency or extra. See
  [cloud identity](docs/operations.md#cloud-identity) and
  [ADR-0017](docs/adr/0017-cloud-identity.md).
- `SIGHUP` re-reads `file:` secrets in a running `serve`, in a thread of its own (signals during a
  reload coalesce, and shutdown does not wait for a read stuck on a mount): provider credentials
  take effect for the next call, and a new database password for the next pooled connection (a DSN
  that changes anything but its password is refused and the old one kept). One `secrets_reloaded`
  log event names the config paths read and changed, never a value. See [rotating
  secrets](docs/operations.md#rotating-secrets).
- `ledger.pool_min`, `ledger.pool_max` and `ledger.pool_timeout_seconds` size the Postgres pool
  (defaults 1, 10 and 10 s, as before).
- `server.keep_alive_seconds` (default 75) and `server.graceful_shutdown_seconds` (default 30),
  passed to uvicorn on the data and admin listeners. Keep-alive was uvicorn's 5 s, below common
  load balancer idle timeouts. The drain after `SIGTERM` now ends at the deadline: calls still
  running are stopped and settled as cancelled (the full hold once sent, unless usage was seen)
  instead of waiting for the orchestrator's kill and the sweeper; their clients get uvicorn's
  plain 500 or a cut stream. A separate admin listener drains in parallel under the same deadline
  ([ADR-0015](docs/adr/0015-readiness-and-shutdown.md)).
- `deploy/compose/compose.yaml` sets `stop_grace_period: 50s` on the `spendgate` service, so
  Docker's 10 s default no longer kills the process in the middle of the 30 s drain.
- The operations guide states what a ledger outage or failover looks like per backend (calls are
  refused with `ledger_unavailable` until the ledger is back, no restart needed) and sizes the
  drain with the new settings.
- Key option `session_required` (`bootstrap.keys[].session_required` in the config,
  `session_required` on `POST` and `PATCH /admin/v1/keys`, shown on every key): a call on such a
  key without `X-Spendgate-Session` is refused with `invalid_request`
  (`param: X-Spendgate-Session`) before the rate limits are counted and before any hold, so an
  agent cannot leave its session limits behind by dropping the header. It needs
  `session_defaults` ([ADR-0013](docs/adr/0013-safer-agent-defaults.md)).
- `spendgate.local.guard(concurrency=...)`: the most calls each client admits at once (16 by
  default, `None` for no bound). With `config=`, the file's `defaults.limits.concurrency` sets it.
- `spendgate migrate --bootstrap` warns when a configured key already exists with other
  `session_required` or `session_defaults` than the config. Bootstrap stays create-only; change
  an existing key with `PATCH /admin/v1/keys/{key_id}`. After upgrading it warns for each existing
  key whose config has `session_defaults` without `max_new_sessions_per_hour`, since the config
  now means 60 there: PATCH the key to adopt the bound, or set `max_new_sessions_per_hour: null`
  in the config to keep it unbounded and silence the warning.
- Optional budget-warning webhook (`alerts.webhook`): `spendgate serve` posts each
  `soft_threshold`, `cap_breached` and `blocked` warning as JSON to one HTTPS endpoint (the log
  line's allowlisted fields plus `event_id` and `ts`), signed with HMAC-SHA256 in
  `X-Spendgate-Signature`. The URL and signing secret are secret references; the URL must be
  `https` (`http` only for localhost) and redirects are not followed. Warnings are queued off the
  request path in a bounded queue and posted by a background task with bounded retries and
  backoff, so a slow or failing receiver never delays or fails a call; deliveries, failures and
  drops are counted in `spendgate_alert_webhooks_total{result}`. See
  [operations](docs/operations.md#budget-warning-webhook) and
  [ADR-0016](docs/adr/0016-alert-webhook.md).
- Ledger health metrics at `/metrics`: `spendgate_ledger_latency_ms{op}` (hold, settle, release,
  lease renewal), `spendgate_ledger_errors_total{op}` (a hold past its deadline counts),
  `spendgate_ledger_unavailable_total`, the Postgres pool (`spendgate_ledger_pool_wait_ms`,
  `spendgate_ledger_pool_size`, `_in_use`, `_max`), `spendgate_sweeper_expired_total` (holds
  charged in full), `spendgate_unsettled_closes`, `spendgate_heartbeat_failures_total{step}` and
  `spendgate_gateway_overhead_ms` (time outside the upstream call). Labels are fixed sets in the
  code. Example Prometheus alert rules are in [operations](docs/operations.md#metrics-and-logs).
  See [ADR-0019](docs/adr/0019-otel-extra.md).
- Optional OpenTelemetry tracing: the `otel` extra and `observability.otel: {enabled,
  service_name}` (off by default). One span per hold, forward and settle, exported over OTLP/HTTP
  as the standard `OTEL_EXPORTER_OTLP_*` variables say; the request's `traceparent` is their
  parent and is passed on to the provider with the forward span. Span attributes are an allowlist
  (request id, catalog model, deployment label, endpoint, outcome, counts, `error.type`); no
  prompt, key, user, tag or session value, and no exception message. Enabled without the extra,
  `serve` warns at start and serves untraced. The base install keeps five runtime dependencies.

### Changed

- Safer defaults for agents ([ADR-0013](docs/adr/0013-safer-agent-defaults.md)):
  `defaults.limits.concurrency` is 16 when the config leaves it out (it was unlimited), also in
  library mode, where each `guard()` client now admits 16 calls at once unless `concurrency=`
  says otherwise (`concurrency=None` for no bound), and
  `session_defaults.max_new_sessions_per_hour` is 60 when a key's session defaults leave it out,
  in the config and the admin API (it was unlimited). An explicit `null` keeps either unlimited,
  and a key in an admin API response shows `max_new_sessions_per_hour: null` when unbounded, so
  sending back what a GET returned keeps it.
  Keys already stored keep the session limits they were created with. The example config has
  an agent key with `session_required` and these limits, and `how-money-is-protected.md` states
  the overrun bound at a cap crossing that the concurrency limit gives.
- `serve` no longer migrates a Postgres ledger at start: on a schema that is behind it refuses
  with exit code 5 and says to run `spendgate migrate` (or start with `serve --migrate`, or set
  `ledger.auto_migrate: true`). SQLite still migrates at start by default. The `--local` admin
  commands follow the same setting, and a ledger error at start is printed as one `error:` line
  instead of a traceback. The Compose and local guides run `migrate` before `up`.
- The `postgres` extra requires `psycopg-pool>=3.3.0` (already installed through
  `psycopg[pool]`): it reads the DSN and the login password per connection.
- Postgres ledger: a call makes 5 round trips to the database instead of about 46 (60 for a
  stream), so a hot budget row stays locked for less time. Statements that do not depend on each
  other go out together (psycopg pipeline mode), `BEGIN` and `COMMIT` ride on the first and last
  of them, `X-Spendgate-Budget-Remaining` comes back with the hold or the settlement instead of
  two or four `FOR UPDATE` reads per call, budget reports read without row locks, lease renewal
  is one statement for all holds, and pooled connections are no longer pinged before each
  transaction (a connection found dead before its `COMMIT` was sent is replaced and the
  transaction re-run). Money rules and the header's meaning are unchanged. See
  [operations](docs/operations.md#postgres-connections) and
  [the load review](docs/reviews/load-v0.1.md).
- `calls.error_code` holds stable, documented codes
  ([ADR-0022](docs/adr/0022-stable-error-codes.md)). Transport and stream failures
  are stored as `connect_error`, `read_timeout`, `write_error`, `remote_protocol_error`,
  `response_too_large`, `stream_malformed`, `stream_limit_exceeded` and so on instead of the
  exception class name (`ConnectError`, `ReadTimeout`, `StreamLimitExceeded`), which could change
  with an HTTP client upgrade. Stop reasons and gateway faults are `StopReason` and
  `AttemptError` enums in `core.failures`, so mypy rejects an unknown one. Every value is listed
  in [data-model](docs/data-model.md#callserror_code-values); existing rows keep their values,
  listed there as legacy. Charges are unchanged.
- Roadmap: adds per-instance budget slices, library mode on Python 3.12, per-end-user budgets
  keyed on `X-Spendgate-User`, weekly and rolling budget windows, a generic OpenAI-compatible
  provider, a read-only usage page and a public hold and settle API, each with why. Soft-threshold webhook alerts and OpenTelemetry tracing move to Done.
- Internal structure, no change in behaviour, config, error codes, headers, endpoints, logs or
  metrics: the ledger port is split into `MoneyLedger` (the eight calls the pipeline and the
  sweeper make) and `AdminLedger` (admin writes, keys and reads on top of it);
  `gateway.compose.build_gateway` is the one composition root, used by `serve` and by the
  integration test harness; OpenAI and Azure OpenAI share one wire module (`openai_wire`);
  `transport()` and `transport2()` share their runtime builder and stream wrappers; SQLite and
  Postgres decide pending migrations with one pure `pending_migrations`; the data app's POST
  routes come from `ENDPOINTS`.
- The size report (`scripts/lightweight_report.py`, `make size`) counts lines on the
  ruff-formatted text, formatted in memory, so packing arguments onto fewer lines no longer meets
  a size limit, and the module (400 lines) and function (50 lines) limits now fail it, as
  ADR-0007 states, instead of warning. `# fmt: skip` is allowed only on a table (a multi-line
  assignment of a list, tuple, set or dict display, or `frozenset` of one, holding only
  constants and names) at module or class level that shares no line with another statement; any
  other, and any `# fmt: off` region, fails the report. The 133 directives outside tables are
  gone. To keep every module within 400 formatted lines and every function within 50, code moved
  with no change in behaviour: the failover loop's results and refusals to
  `pipeline.forward_results`, the building of a `Pipeline`'s parts to `pipeline.assembly`, the
  settings types to `config.settings`, and two steps of `ledger.settle_flow` into helpers of
  their own.

### Fixed

- A Gemini answer whose parts carry no billed output (thought parts, empty texts, thought
  signatures) and no `candidatesTokenCount` is charged its real usage instead of the full hold.
  Gemini bills thoughts in `thoughtsTokenCount`, which such an answer must then carry when it
  shows thinking; any other part, including one of a kind the adapter does not know, still
  needs the candidates count, and content after the finish still makes the usage untrusted.
  Never an under-charge (M-13) (#67).
- `/readyz` no longer answers 503 while a global kill switch is engaged. The switch is shared, so
  every replica turned unready at once and load balancers dropped them all. Calls are still
  refused with `kill_switch` (503); the admin readiness detail lists the switch as a warning
  instead of a reason, and a failed read of it as the warning `kill_switch_probe_failed`
  (ADR-0015).
- The `http://`-only-for-localhost rule for a provider `base_url` reads the host the way the HTTP
  client does: a `?` or `#` ends the host part, so text after one is never taken for it, and a
  URL with whitespace or a non-printable character is refused.
- On Postgres, holds and releases on one session no longer deadlock. A release locked the budgets
  before the session row it refunds a step to, against the documented lock order (session row,
  then budgets) that holds follow, so concurrent calls on a session hit 40P01 retries, each
  costing Postgres's `deadlock_timeout`, until some ran out of retries and answered 503. Release
  now locks the session row first, in the same batch, with no extra round trip (#66).
- Provider throttling no longer turns into an outage (issue #36). A 429 without `Retry-After`
  cooled its deployment for the full `routing.cooldown_seconds` and then let one probe through,
  so in the load test's clean scenario (3% injected 429s) a fifth to a third of all calls were
  refused with `503 upstream_unavailable`; now none are. A busy 429 cools for its `Retry-After` as given
  (capped by `max_cooldown_seconds`; an HTTP-date or a `retry-after-ms` is now read too) or,
  without one, for a random 250 to 500 ms that doubles for each 429 in a row (up to
  `cooldown_seconds`) until a call sent after the throttle succeeds, and the deployment is open
  to every caller once that ends. A quota 429 or an unreadable
  `Retry-After` cools for `cooldown_seconds`. A call that finds every deployment cooling on a
  first busy 429 waits up to 1 s (plus its own jitter, inside its one hold, and only with a full
  1 s left before its deadline) for the soonest to come back; an escalated cooldown or a breaker
  opened on failures is never waited for. A busy cooldown keeps the breaker's failure window. A result
  that arrives while its deployment cools no longer turns an `upstream_throttled` refusal into
  `upstream_unavailable`, nor closes a failure-opened breaker without its probe.
  See [ADR-0021](docs/adr/0021-short-throttle-cooldown.md).
- A `file:` secret that is not UTF-8 text is a configuration error naming the file ("secret file …
  is not UTF-8 text", exit code 3 for `check-config`, `serve` and `migrate`) instead of a
  traceback, and a hot reload keeps the old value. The decode error, which quotes the file's
  bytes, is never shown or chained (#64).

## [0.1.3] - 2026-11-24

### Added

- Weekly fuzz workflow (`.github/workflows/fuzz.yml`): every `tests/fuzz` target in long mode
  for 60 seconds with a new seed each run, one JSON report per target uploaded.
- Budget warnings: one WARNING log line on the `spendgate.budget` logger when a budget crosses its
  soft threshold (`soft_threshold`), when an overrun takes it above its cap (`cap_breached`) and
  when that breach blocks a `strict_overrun` budget until `ack-overrun` (`blocked`). They come
  from what `hold` and `settle` return: `soft_threshold` once per budget period, `cap_breached`
  once per crossing of the cap, `blocked` once per review flag. Budget facts only (`budget_id`,
  `scope_path`, `scope_kind`, `period`, `period_start`, `spent`, `cap`, `currency`); a session
  budget is named by its key's scope, never by the session or key id. Before,
  the soft threshold was only stored and a breach was only a metric. See
  [operations](docs/operations.md#metrics-and-logs).

### Changed

- `make test` now enforces the coverage floors CONTRIBUTING.md documents: >= 90% over
  `src/spendgate`, then `core/` >= 95%, `ledger/` >= 90% and `pipeline/` >= 90% each
  (`make coverage-gates`). Before, only the 90% aggregate was checked.
- `make test-postgres` (CI's integration job) measures coverage and holds the Postgres ledger
  backend (`ledger/postgres*.py`) to >= 90% on the Postgres run; it now refuses to start without
  `SPENDGATE_TEST_POSTGRES_URL`.
- Tests from the v0.1 review rounds (`test_review_fixes.py`, `test_verification_fixes.py`,
  `tests/load/test_known_defects.py`) now live in files named for the behaviour they cover
  (intake memory, intake stalls, close retry, charge after send, request deadlines, unread
  streams, bad input). Every test kept its name, and its review id is in its docstring.
- `Gate.buffered_bytes` and `Pipeline.instance`, `owned()` and `unsettled()` are read-only
  probes, and `Pipeline.drain()` waits for background settlements (`aclose` uses it), so tests
  no longer read private attributes. The stalled-upload tests wait on an event set by the fake
  body instead of sleeping 10 ms, and the settlement tests await `drain()` instead of polling.
- `BudgetExhausted.hold` in the Python SDK: the refused call's worst-case hold from
  `details.hold`, parsed like `remaining`.
- The admin API refuses to create a budget whose `model_filter` matches no routed catalog id
  (`invalid_request`, `param: model_filter`): such a budget would never apply. The filter is a
  case-sensitive glob over the catalog id (such as `openai/gpt-6-*`), never the alias; the
  configuration reference, the OpenAPI document and the operations guide now say so
  ([ADR-0011](docs/adr/0011-model-filter-validation-and-late-settle-reservation.md)).
- `spendgate check-config` and `spendgate serve` warn about each `bootstrap.budgets` entry whose
  `model_filter` matches no routed catalog id. A warning, not an error: the configuration still
  loads (ADR-0011).

### Fixed

- The load crash scenario could fail when the killed gateway owned no pending hold. It now waits
  until the victim holds pending reservations of its own, checks that each of them was swept,
  and runs the `serve` command as shipped.
- Runaway-loop tests T-SESS-02 and T-SESS-03 in `tests/integration/test_sessions.py`: 40
  parallel loops on one key stop at the budget cap on SQLite and Postgres (nothing over the cap,
  nothing left held, every refusal `budget_exhausted`), and a kill switch engaged mid-loop refuses
  the next call and stops an open stream at the next heartbeat. The threat model's C-18 row now
  points at the files that carry each T-SESS id.
- An unexpected exception that a catch-all turned into `500 internal_error` (or into a refusal
  before the hold) left no log record. It now writes one `spendgate.internal` JSON log event with
  the exception class, the frames as `module:function:line`, the catch-all's tag and the request
  id, never the message. `charged: true` shows a call that was charged although the client got
  the error. Library mode logs the same facts on the `spendgate.local` logger. Logging only:
  responses and charges are unchanged. An independent fix was also proposed in #42; thank you.
  responses and charges are unchanged.
- A `budget_exhausted` or `budget_period_exhausted` refusal from a budget that still has room no
  longer says the budget is exhausted. The message now says the call's worst-case hold exceeds
  the remaining budget and suggests setting or lowering `max_tokens` (or `max_output_tokens`) or
  raising the cap; without `max_tokens` the hold uses the catalog's output ceiling, so a 5 USD cap
  could refuse a flagship call with 4.99 USD left. The code and status are unchanged, and
  `details.hold` now carries the hold in the same format as `details.remaining`. A budget with
  nothing left keeps the old message.
- `spendgate_requests_total` on refused calls no longer takes its `model` label from the request
  body. It is the routed model's catalog id, as on settled calls, or `unknown` when the request
  names no routed model, so clients cannot create a new series per model string they send.
- A settle that arrives after the sweeper expired its hold now also updates the reservation's
  `charged_nano` (as the ledger's `get_reservation` reads it) to the hold plus the late overrun,
  in the same transaction. Before, the reservation kept showing only the swept hold
  while the budget and the call row already carried the overrun. Reporting only: the money
  booked to the budgets was already correct (ADR-0011).
- `spendgate --version` prints the installed version.
- Admin commands without `--local` or `--url` now find the admin API from `--config`
  (`server.admin.listen`, else `server.listen`) instead of assuming `127.0.0.1:8081`, which the
  README config does not use. `SPENDGATE_URL` and `--url` still take precedence. An address from
  the config is used only when it is on loopback; for any other host pass `--url`.
- `spendgate status --local` and the admin API report the installed version, not `0.0.0`.
- `spendgate keys list` (and `keys create`) show the key's scope path, such as `org:demo`, not
  the scope's id. `--json` output is unchanged.
- `serve`, `migrate` and `check-config` no longer accept or list `--url`, `--local` and `--json`,
  which never applied to them.
- `check-config` and start-up print each configuration problem once: deployments sharing a
  provider credential no longer repeat it, and an unpriced routed model is no longer reported
  again as a warning.
- The hint after `kill on` repeats the `--local`, `--config` and `--url` flags the command was
  given (without any user name or password in the URL), so `kill off` reaches the same ledger or
  API.
- PyPI project links (homepage, documentation, changelog, issues) and classifiers; CI and PyPI
  badges on the README.
- The release workflow creates the GitHub Release for the tag, with the tag's CHANGELOG section
  as its notes.
  as its notes (marked as a pre-release for `aN`, `bN` and `rcN` versions). An empty section stops
  the release before anything is published.
- README links are absolute, so they work on the PyPI project page.
- Docs: the local guide documents the signed GHCR images (`:<version>` and `:<version>-full`),
  how to verify them with cosign, and using them with Compose instead of building.
- Docs: the LiteLLM comparison covers end-user and team budgets, budget windows, soft-budget
  alerts, admin UI and SSO, guardrails and caching, budgets without a database server and supply
  chain, with links to LiteLLM's documentation, and a section on running spendgate in front of
  LiteLLM.
- Docs: the README quickstart config sets an output ceiling (`defaults.max_output_tokens`) and a
  separate admin listener (`server.admin`), and every `create()` sample passes `max_tokens`. The
  README states the catalog size, links how to add models, and says the image needs no Python
  3.13 on the host.
- Docs: the local guide installs from PyPI (or `uvx`) instead of saying the package is not
  published; stale paths and wording fixed in the catalog schema page, the design summary (name
  check done), decision D4 (`ledger.currency` is required) and the example config header (which
  secrets must exist before `check-config`).
- An import-linter contract, "only the gateway imports Starlette", so no package but
  `spendgate.gateway` can import Starlette directly.
- Import-linter contracts "only the gateway imports Starlette" and "only cli.serve imports
  uvicorn": allow-lists over the whole package, so a new module is covered at once.
- Operations guidance for draining on restart (a `preStop` wait, a grace period sized to
  `server.max_request_seconds`, and the lease and sweep window after a kill) and for a verified
  TLS Postgres connection (`sslmode=verify-full`).
- Library mode: why an autonomous agent with shell access can undo it, and to use the proxy with
  the perimeter instead.
- The docs now match the code: the request path authenticates before it reads the body (SR-01),
  steps 3 to 6 run in `pipeline/steps.py` on `core` rules, the modules that import Starlette are
  listed, and the architecture status and ADR list are current for v0.1.1.
- Session limits are documented as applying to calls that send `X-Spendgate-Session`, with the
  parent budgets as the backstop (README, design summary, threat model TH-09). Overruns are
  described as recorded, exposed as metrics and shown in status. The README states the current
  product size.
- Credential refreshes (Entra ID for Azure OpenAI, the Vertex AI service-account token, the AWS
  credential chain for Bedrock) no longer run on the event loop: a slow identity provider stalled
  every request the gateway was serving. Adapters now refresh in a worker thread from a new async
  `Provider.prepare` step, one refresh per credential however many calls are waiting. A failed
  refresh is still an internal error and nothing is sent.
- Credential refreshes (Entra ID for Azure OpenAI and Azure Speech, the Vertex AI
  service-account token, the AWS credential chain for Bedrock) no longer run on the event loop: a
  slow identity provider stalled every request the gateway was serving. Adapters now refresh in a
  worker thread from a new async `Provider.prepare` step, one refresh per credential however many
  calls are waiting or give up. A failed refresh is still an internal error and nothing is sent.
  Azure Speech now caches its Entra token until shortly before it expires, and Bedrock drops its
  credentials snapshot after a 401 or 403.
- A call no longer waits indefinitely on the ledger. The hold transaction is bounded at 10 s and
  then refused with `ledger_unavailable` (503) without contacting the provider; a hold that
  commits after that is released at 0. Postgres connections get `lock_timeout` 2 s,
  `statement_timeout` 5 s (60 s for usage reports, call and audit lists and retention batches),
  `idle_in_transaction_session_timeout` 10 s, TCP keepalives and `tcp_user_timeout` 30 s, unless
  the DSN or the server already sets them; a lock or statement timeout is not retried. Lease
  renewal skips a hold whose row another transaction has locked instead of waiting for it. See
  [Postgres connections](docs/operations.md#postgres-connections) and
  [ADR-0012](docs/adr/0012-bounded-ledger-waits.md).
- The pipeline verifies a request's key once. It verified it a second time after reading the
  body, with a later clock reading. After the body is read it now only re-checks, through the key
  cache, that the same key is still unrevoked and unexpired; a key revoked or expired during the
  upload is refused with `unauthorized` before anything is held.
- Cached scope chains expire after 30 seconds, so a scope moved to a new parent reaches the budget
  headers and settled events after that time, and streams opened after it, without a restart. A
  stream already open keeps its chain until it ends.
- The heartbeat is supervised in the gateway and in library mode: a step that raises (lease
  renewal, the stream watch, the library-mode sweep) is logged as plain text on the
  `spendgate.pipeline` logger and retried at the next beat. In the gateway one error ended the
  heartbeat for good; library mode swallowed errors without a trace and skipped the stream watch
  when renewal failed.

## [0.1.2] - 2026-11-24

### Changed

- CI and the commit-msg hook also reject a commit whose author or committer is a coding
  assistant's own git identity (`scripts/check-commit-identity.sh`), next to the existing check on
  commit messages and pull request text.

### Security

- A stream that is cut, cancelled by the client or stopped by the gateway (kill switch, lease,
  deadline) is settled only from its final, end-of-stream usage, else at the full hold (M-13).
  A Gemini stream was settled at the last running total from a mid-stream chunk, which is partial;
  its usage now counts only when sent with (or after) the finish reason. The OpenAI and Azure
  OpenAI stream readers likewise take usage only from the usage-only chat chunk or a terminal
  Responses event, not from running totals on content chunks, and drop it again if more answer
  follows it. An OpenAI-compatible upstream must therefore send the usage-only chunk (the gateway
  forces `stream_options.include_usage` upstream); a stream without it is charged the full hold.
  A Gemini finish reason counts only if it is a known terminal one (not
  `FINISH_REASON_UNSPECIFIED`), and content after the finish makes its usage untrusted again.
- Provider usage that lacks a required counter is now unreadable, so the call is charged the full
  hold. OpenAI and Azure OpenAI chat completions without `completion_tokens`, Responses and
  token-billed transcriptions without `output_tokens`, embeddings without `prompt_tokens`, and
  Gemini answers without `promptTokenCount` (or without `candidatesTokenCount` when a candidate
  carried content) were read as 0 for the missing counter and could settle at or near 0.
  Optional counters (cached, reasoning, audio) must be non-negative ints when present, and a
  speech request whose input is not text has no usage. A streamed Gemini answer is judged against
  all content seen, not only the chunk that carried the totals.
- OpenAI and Azure OpenAI usage is read in the shape of the request's endpoint only: a
  transcription `duration` usage on a chat, Responses or embeddings answer is unreadable. A
  streamed usage object that does not parse makes the stream's usage unreadable instead of
  leaving an earlier one in place. A Gemini `totalTokenCount` larger than the sum of its parts
  is unreadable, as are streamed Gemini totals that drop below earlier ones (the client then gets
  no usage chunk either).
  A null optional counter is unreadable unless it can only lower the cost: a null cache-read
  count is read as 0.
- Inline media must be a base64 png, jpeg, webp or gif image. A data URL of another type (video,
  audio, PDF) in an `image_url` or `input_image` part, or an Anthropic `image` block whose
  `source.media_type` is not one of those four, is now refused `invalid_request` before routing.
  Such media was held as one image while the provider (Gemini in particular) billed the whole
  file. `fail_open.allow_remote_media` admits only `http(s)` URLs in image positions and never
  other inline media. On Bedrock, `data:image/jpg` is no longer accepted (use `image/jpeg`); Gemini
  accepts png, jpeg and webp only; the base64 payload must be on one line everywhere; Responses
  `instructions` must be a string.
- Each image part is now held at the catalog entry's new optional `limits.max_image_tokens`, or
  61,000 input tokens when unset: the largest per-image cost a provider documents plus 25
  percent. The flat 1,536 tokens held before was below what current models bill for one large
  image (up to 36,000 on `gpt-6-astra`). Bundled entries set cited values: 45,000 for
  `gpt-6-astra`, 6,000 for Claude (Anthropic and Bedrock), 1,400 for Gemini; the other OpenAI
  and Azure OpenAI entries keep the default. Image holds are therefore much larger than before:
  the same worst case counts against a key's `tpm` limit, and 4 images (5 on `gpt-6-astra`) push a
  request over the 272,000-token long-context threshold on OpenAI and Azure OpenAI, since the tier
  is taken at 80% of it, which prices the hold at the long-context rates.
- An object labelled as an image or audio part is held as one only where the request accepts
  content parts (a message's `content`, a Responses `function_call_output.output`, an Anthropic
  `tool_result.content`). Elsewhere, for example in tool input or tool call arguments, it is now
  counted as text.
- The input-token estimate used for holds now covers text that tokenizers split finely. Words that
  mix letters and digits or change case (identifiers, hex, UUIDs, base64), and words with two or
  more capitals or no vowel, are counted piece by piece; digits and accented letters at one token
  per UTF-8 byte; other words at one token per two letters; CJK at five tokens per two characters.
  A space is free only before an ASCII letter, a Latin word, ASCII punctuation or a CJK run, not
  before a number, a control character, a lone rare character or the end of the text. Only runs of
  one kind of whitespace (spaces, tabs, `\n` or `\r\n` lines with indentation) are discounted;
  `\r`, `\x0b`, `\x0c`, control characters and mixed whitespace cost one token per character.
  Before, such text could be counted at a third of its real size. Natural and random text is now
  at or above the o200k and cl100k counts after the 25% margin; text chosen to tokenize badly can
  still reach about 1.6x the margined estimate (never more than one token per UTF-8 byte), and the
  excess is booked as overrun and blocks the budget under the default `strict_overrun`. Input
  estimates grow by about 30% for English prose (38% with capitalised passages), 21-25% for
  Python, 34-53% for JSON, 42% for Portuguese, 51% for Spanish and 71-73% for French and German
  (money rule M-14).
- The hold now takes a model's long-context tier once the input estimate, which already carries
  the 25% margin, reaches 80% of the threshold (`input_tokens x 1.25 >= threshold`), or once a
  byte bound does (new `Estimate.input_tokens_bound`): the UTF-8 bytes of the input text, which no
  byte-level tokenizer exceeds, plus the overheads and the catalog image ceiling per image, which
  are assumptions about hidden prompts and image billing. Before, a call estimated just under the
  threshold but billed above it was held at the short-tier price. For English prose the byte bound
  fires first, from about 20% of the threshold in real tokens (about 20k of 100k, 55k of 272k);
  Markdown with tables and code from about 26%.
- Anthropic `tool_use.input` is counted as JSON text, image-shaped objects in it included (its
  punctuation and nulls too; the byte bound takes the indented, ASCII-escaped rendering).
  Before, `{"rows": [null, ...]}` was nearly free. Numbers, booleans and nulls elsewhere in
  messages are counted as the text they serialise to, not as one token or none.
- Opaque inputs the validators accept (`thinking.signature`, `redacted_thinking.data`, Responses
  `reasoning.encrypted_content`) are counted at their bytes, without the margin.
- A JSON-schema or JSON-mode output format (`response_format`, `text.format`,
  `output_config.format`) adds 600 tokens for its hidden prompt.
- Requests with `tools` add 600 tokens plus 32 per definition to the input estimate, for the
  hidden prompt that providers wrap around tool definitions.
- Every adapter now forwards exactly one output ceiling, the one the hold was sized with. A chat
  request that named both `max_tokens` and `max_completion_tokens` was held at the smaller value
  while OpenAI and Azure OpenAI received both fields and honour `max_completion_tokens`, so the
  reply could run past the hold. The client's ceiling fields are now dropped and the held ceiling
  is sent alone (`max_completion_tokens` for chat, `max_output_tokens` for Responses, `max_tokens`
  for Messages, `maxTokens` and `maxOutputTokens` on Bedrock and Gemini); it is still added when
  the request names none. New money rule M-15. OpenAI and Azure OpenAI upstreams, including
  OpenAI-compatible servers behind `base_url`, must honour `max_completion_tokens`; on Azure the
  v1 API is recommended.

## [0.1.1] - 2026-11-20

### Fixed

- `X-Spendgate-Budget-Remaining`, `X-Spendgate-Budget-Scope` and the
  `spendgate_budget_remaining_nano` gauge now consider only the budgets the call's hold took. A
  budget whose `model_filter` does not match the call's model was reported as the call's tightest
  budget. Reporting only: holds, refusals and charges were already correct.

## [0.1.0] - 2026-11-20

### Added

- OpenAI- and Anthropic-compatible proxy (`spendgate serve`) for chat completions, Responses,
  embeddings, audio transcription and speech, with SSE streaming.
- Provider adapters: OpenAI, Azure OpenAI, Anthropic, AWS Bedrock (Converse), Google (Gemini API
  and Vertex AI), Azure Speech, ElevenLabs.
- Hard budgets per organisation, project, service, user and agent session: the worst-case cost is
  held before forwarding and settled exactly once; overruns are recorded and block by default.
- Fail closed: unpriced models, missing exchange rates or output ceilings, an unreachable ledger
  and kill switches refuse before any provider call.
- Provider throttling: free failover between deployments of the same model within one hold.
- Ledgers on SQLite and Postgres with a shared contract suite; virtual keys with rotation and
  revocation; usage queries and retention; a background sweeper for abandoned holds.
- Price catalog as data (`catalog/prices.yaml`) with a JSON Schema.
- Admin API (`/admin/v1`) and `spendgate` CLI; config bootstrap.
- SDK helpers for the OpenAI and Anthropic SDKs, and an in-process library mode
  (`spendgate.local.guard`).
- Prometheus metrics, JSON logs and health endpoints.
- Release image (`core` and `full`) and a Docker Compose reference setup with a network perimeter.
- Documentation, including how money is protected, a configuration reference and a tested local
  guide.

### Security

- Fixes from the v0.1 code, security and verification reviews and from fuzzing
  (`docs/reviews/`); behaviour changes are recorded in ADR-0009.
