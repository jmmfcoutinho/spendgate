# Live smoke test v0.2 (2026-11-25)

Second round of calls to real providers (issue #9), for the adapters [v0.1](live-smoke-v0.1.md)
could not reach: OpenAI chat, Responses and embeddings, Anthropic, and Google through both the
Gemini API and Vertex AI, and [AWS Bedrock](#aws-bedrock-2026-11-26) the next day. Code at 0.2.0
(`ab6e2b9`; Bedrock at 0.3.0), through a local `spendgate serve` (SQLite
ledger in EUR, one budget of 1 EUR, FX USD→EUR fixed at a conservative 0.95). Provider credentials
came by `file:` reference (Vertex by `credential: adc`); the client held only spendgate keys.

**Total spend: 0.000255056 EUR** (ledger, 16 calls), nothing left held (every reservation settled).

| Call | Provider / route | Result | Tokens in / out | Charged (EUR) |
|---|---|---|---|---|
| Model list | gateway `/v1/models` | 200 | — | 0 |
| Chat completion | OpenAI `gpt-6-luna` | 200, "OK" | 13 / 15 (5 reasoning) | 0.000008360 |
| Chat completion, streamed | OpenAI | 200; injected usage chunk hidden | 13 / 15 | 0.000008360 |
| Chat, function tool, `reasoning_effort: "none"` | OpenAI | 200, `get_weather` call | 134 / 18 | 0.000021280 |
| Responses, function tool | OpenAI | 200, `function_call` | 52 / 19 | 0.000013965 |
| Responses | OpenAI | 200, "OK" | 13 / 5 | 0.000003610 |
| Responses, streamed | OpenAI | 200 | 13 / 15 (8 reasoning) | 0.000008360 |
| Embeddings, two inputs | OpenAI `text-embedding-3-small` | 200, two vectors | 4 / — | 0.000000076 |
| Messages | Anthropic `claude-haiku-5-5` | 200, "OK" | 18 / 4 | 0.000003610 |
| Messages, streamed | Anthropic | 200 | 18 / 4 | 0.000003610 |
| Messages, `tool_use` | Anthropic | 200, `get_weather` call | 380 / 52 | 0.000060800 |
| Chat completion | Google `gemini-3.5-flash-lite`, Gemini API (`x-goog-api-key`) | 200, "OK" | 8 / 1 | 0.000004655 |
| Chat completion, streamed | Google, Gemini API | 200 | 8 / 1 | 0.000004655 |
| Chat, function tool | Google, Gemini API | 200, `tool_calls` | 53 / 17 | 0.000055480 |
| Chat completion | Google, Vertex AI (`auth: bearer_token`, `credential: adc`, `region: global`) | 200, "OK" | 7 / 1 | 0.000004370 |
| Chat completion, streamed | Google, Vertex AI | 200 | 7 / 1 | 0.000004370 |
| Chat, function tool | Google, Vertex AI | 200, `tool_calls` | 32 / 17 | 0.000049495 |
| Chat on a key whose budget is 0.0001 EUR | — | 402 `budget_exhausted`, no upstream call | — | 0 |
| Model not on the key's list | — | 403 `model_not_allowed`, no upstream call | — | 0 |

Every charge was checked by hand against tokens × catalog price × 0.95 and matches to the nano-unit
(for example the Anthropic tool call: (380 × 0.1 + 52 × 0.5) USD/Mtok = 0.000064 USD, × 0.95 =
0.0000608 EUR). Output tokens include reasoning, priced at the output rate as the catalog says.

## Fixtures refreshed from the live APIs

The same requests were sent once more straight to each provider (the bodies spendgate builds,
including the `store: false` and `stream_options` it adds), and the answers were recorded as
contract fixtures, with ids, thought signatures and encrypted reasoning replaced by fakes and
embedding vectors cut to three values. They sit next to the documented fixtures, which stay for the
edge cases a short "OK" answer cannot show (cache reads and writes, thinking, an id-less call):

- **OpenAI** (`tests/contract/providers/openai/fixtures/*_live*`, provenance in `MANIFEST.json`):
  chat and Responses, each plain and streamed (raw SSE bytes), and embeddings. Real answers carry
  `cache_write_tokens` in the input details (already read), and the Responses stream an encrypted
  reasoning item before the message.
- **Anthropic** (`*_live_20261125.*`): a message, a `tool_use` answer and a stream.
  `message_delta` repeats the input and cache counts, usage has `output_tokens_details`, `tool_use`
  blocks carry a `caller`, and SSE data lines are padded with spaces; the relay passes them through
  byte for byte.
- **Google** (`gemini_*_live_20261125.*`, `vertex_*_live_20261125.*`): a text answer, a function
  call and a stream from each API. Text parts carry thought signatures, function calls carry an
  `id` (kept as the OpenAI call id), Vertex adds `trafficType` and `createTime`, the first Vertex
  stream chunk has a `usageMetadata` without counts, and the Gemini API sends running totals on
  every chunk. All convert and give the billed usage.

No real answer showed a shape the adapters mishandle.

## Findings

None is a money or budget problem. Findings 6 to 9 come from the Bedrock run below.

1. **Gemini API prepaid credit.** The first Gemini API calls got a provider **402
   `RESOURCE_EXHAUSTED`**: the AI Studio account had no prepaid credit. spendgate settled them at 0
   as provider rejections without usage, which is right, since nothing was processed. After a
   top-up every call passed. The Google adapter classifies that 402 as `invalid_request` (the
   client gets 400 "The provider rejected the request." and no failover). The
   [protocol](../protocol.md) says a provider 402/403 on our key is `upstream_unavailable` (502,
   provider named), as the Anthropic adapter does: see follow-ups.
2. **`gpt-6-luna` tools on chat completions.** `/v1/chat/completions` rejects function tools unless
   `reasoning_effort` is `"none"` (OpenAI 400, `param: reasoning_effort`); with it, tools work.
   `/v1/responses` accepts tools directly. spendgate relayed the 400 as `invalid_request` and charged
   0, as designed.
3. **Vertex prices are unverified.** Vertex calls are priced with the Gemini API catalog entry: the
   catalog has no Vertex-specific prices (see the comment in `src/spendgate/catalog/prices.yaml`).
   The charges above are right for the Gemini API price list; whether Vertex bills the same for
   this model is open.
4. **Short key pepper.** A pepper shorter than 16 bytes passes `check-config`, but `serve` and the
   `--local` commands then stop with a traceback. Reported separately.
5. **Operator note: OpenAI 4xx messages are withheld.** The client sees only "The provider rejected
   the request." for a provider 4xx, by design (prompts can echo in provider messages); finding 2
   needed a direct call to OpenAI to read the reason. The provider's status is in
   `calls.http_status`.

## AWS Bedrock (2026-11-26)

Run on a second machine with AWS access, in **eu-west-1**, with spendgate 0.3.0 (`v0.3.0`) and the same
setup (SQLite ledger in EUR, one budget of 1 EUR, FX 0.95). Bedrock signed with the standard AWS credential
chain (`auth: sigv4`); the client held only spendgate keys. Claude is reachable in eu-west-1 only through
inference profiles (no Claude foundation model is listed as on-demand there), so each deployment set
`model:` to the `eu.` profile id.

**Total spend: 0.013678822 EUR** (ledger, 9 calls), nothing left held. Almost all of it is the
placeholder price below; the real Bedrock cost was a small fraction of that.

| Call | Model (deployment `model`) | Result | Tokens in / out | Charged (EUR) |
|---|---|---|---|---|
| Chat completion | `eu.anthropic.claude-haiku-5-5`, priced as `bedrock/anthropic.claude-haiku-5-5` | 200, "OK" | 18 / 4 | 0.000003971 |
| Chat completion, streamed | Haiku 5.5 | 200 | 18 / 4 | 0.000003971 |
| Chat, function tool | Haiku 5.5 | 200, `get_weather` call | 380 / 52 | 0.000066880 |
| Chat, plain / streamed / tool | `eu.anthropic.claude-sonnet-5-5`, placeholder price | 200 each | 18 / 4, 18 / 4, 380 / 52 | 0.000361, 0.000361, 0.006080 |
| Chat, plain / streamed / tool | `eu.anthropic.claude-opus-5-5`, placeholder price | 200 each | 18 / 4, 18 / 4, 380 / 52 | 0.000361, 0.000361, 0.006080 |
| Chat on a key whose budget is 0.0001 EUR | — | 402 `budget_exhausted`, no upstream call | — | 0 |
| Model not on the key's list | — | 403 `model_not_allowed`, no upstream call | — | 0 |

Haiku is priced with the bundled catalog entry (0.11 / 0.55 USD per Mtok, the us-east-1 regional price):
(18 × 0.11 + 4 × 0.55) USD/Mtok × 0.95 = 0.000003971 EUR, and the tool call 0.000066880 EUR, both exact to
the nano-unit. The catalog has no Bedrock entry for Sonnet or Opus, so the run added two override entries
(`catalog.paths`) at a deliberately high placeholder price (10 / 50 USD per Mtok, `verified: false`); their
charges match that price exactly, which checks the adapter and the arithmetic, not a real price. The
streamed charge equals the plain one, so ConverseStream's final `metadata.usage` is read.

**Fixtures.** `tests/contract/providers/bedrock/fixtures/` now holds a Converse text answer, a tool-use
answer and the exact 880 bytes of a ConverseStream answer (event-stream frames with their CRCs), with
`tests/contract/providers/bedrock/test_live_fixtures.py` reading them. Real details they cover: a `type`
field on `toolUse` blocks, a `p` padding field on every stream payload, no `contentBlockStart` before a
text delta, and a stream `metadata.usage` without cache counts (and with `serverToolUsage: {}`) where the
non-stream usage has both at 0. All decode and price correctly.

**Findings (Bedrock), none a money problem:**

6. **Bedrock errors on our credentials were a 500.** A dry run with invalid AWS credentials, made while
   preparing this run, got Bedrock's 403 back as `internal_error` (500, "The provider rejected the
   gateway."), not `upstream_unavailable` (502) as the protocol says; fixed in #98. The charge was 0.
7. **Prices.** The Haiku 5.5 entry is the us-east-1 regional price, applied here to the `eu.` profile in
   eu-west-1; whether that matches the EU price was not checked. Sonnet 5.5 and Opus 5.5 have no Bedrock
   price, so spendgate refuses them (`model_not_priced`) until they are added.
8. **Model ids.** The deployable ids are inference-profile ids with no version suffix
   (`eu.anthropic.claude-haiku-5-5`, `global.anthropic.claude-haiku-5-5`). The catalog id
   `bedrock/anthropic.claude-haiku-5-5` works for them with an explicit deployment `model:`.
9. **Tooling note.** AWS CLI v2 has no event-stream operations (`aws bedrock-runtime converse-stream` does
   not exist), so the raw stream was captured with a SigV4-signed POST instead.

## Not covered

- OpenAI speech (no catalog price), Azure OpenAI transcription and OpenAI `whisper-1`.

## Follow-ups

- **Google adapter:** map a provider 402 (and other billing or quota errors on our key) to
  `upstream_unavailable`, per the protocol, with a contract test. Done in #93.
- **Bedrock adapter:** the same for a 401/403/404 on our AWS account (#98).
- **Catalog:** verify Vertex AI prices for the Gemini models, or encode Vertex entries. Done in #107:
  Region-scoped Vertex entries generated from the Google Cloud Pricing API by
  `scripts/vertex_prices.py`. On the global endpoint used above, Gemini 3.5 Flash-Lite costs the same
  on Vertex as on the Gemini API (0.30 input, 0.03 cached, 2.50 output per MTok), so the charges in
  the table stand; the non-global endpoints are 10% dearer.
- **Catalog:** Bedrock prices for EU inference profiles, and Bedrock entries for Sonnet 5.5 and Opus 5.5.
  Done in #100: every geographic and global profile, and the EU price equals the US one.
