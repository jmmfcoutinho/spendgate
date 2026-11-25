# Live smoke test v0.2 (2026-11-25)

Second round of calls to real providers (issue #9), for the adapters [v0.1](live-smoke-v0.1.md)
could not reach: OpenAI chat, Responses and embeddings, Anthropic, and Google through both the
Gemini API and Vertex AI. Code at 0.2.0 (`ab6e2b9`), through a local `spendgate serve` (SQLite
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

None is a money or budget problem; one needs a small adapter fix.

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

## Not covered

- **AWS Bedrock**: no AWS credentials on this machine; to be run on a machine with Bedrock access.
  Its adapter is still tested against documented shapes only.
- OpenAI speech (no catalog price), Azure OpenAI transcription and OpenAI `whisper-1`.

## Follow-ups

- **Google adapter:** map a provider 402 (and other billing or quota errors on our key) to
  `upstream_unavailable`, per the protocol, with a contract test.
- **Catalog:** verify Vertex AI prices for the Gemini models, or encode Vertex entries.
- **Bedrock:** a live smoke run and fixtures from real Converse answers (issue #9).
