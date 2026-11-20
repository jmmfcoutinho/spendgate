# Live smoke test v0.1 (2026-11-17)

First calls to real providers, through a local `spendgate serve` (SQLite ledger in EUR, budget
5 EUR, FX USD→EUR fixed at a conservative 0.95). Provider keys came from the environment
by `env:` reference only; the client held only spendgate keys.

**Total spend: 0.003369170 EUR** (ledger), nothing left held.

| Call | Provider / route | Result | Charged (EUR) |
|---|---|---|---|
| Chat completion | Azure OpenAI `gpt-6-luna` (`/openai/v1`) | 200, "OK" | 0.000010032 |
| Chat completion, streamed | Azure OpenAI | 200; injected usage chunk hidden; cost comment before `[DONE]` | 0.000003762 |
| Responses | Azure OpenAI | 200 | 0.000004332 |
| Responses, streamed | Azure OpenAI | 200 | 0.000004332 |
| Chat completion via the OpenAI SDK helper (`spendgate.client.openai`) | Azure OpenAI | 200, "OK" | (in the chat total) |
| Speech (TTS), 2 calls | ElevenLabs `eleven_flash_v2_5` | 200, MP3 audio | 0.002660000 |
| Transcription of that audio | Azure Speech fast transcription | 200, correct text | 0.000356250 |
| Transcription of that audio | OpenAI `gpt-transcribe` (temporary catalog override, deliberately high price) | 200, correct text | 0.000316668 |
| Chat completion | OpenAI `gpt-6-luna` | provider 4xx → 502 `upstream_unavailable`, free | 0 |
| Embeddings | OpenAI `text-embedding-3-small` | provider 4xx → 502, free | 0 |
| Transcription | Azure OpenAI `whisper` | provider 4xx → 502, free | 0 |
| Chat on a key whose budget is 0.0001 EUR | — | 402 `budget_exhausted`, no upstream call | 0 |
| Model not on the key's list | — | 403 `model_not_allowed`, no upstream call | 0 |

The three provider 4xx answers are explained by the keys, not by spendgate. The ledger's
`calls.http_status` shows **403** for OpenAI chat and embeddings (the OpenAI key is limited to audio
models; its transcription call succeeded) and **404** for Azure `whisper` (the resource has no such
deployment). Each was settled at 0 as a provider rejection without usage, as designed.

Azure's first streamed chat chunk carries `prompt_filter_results` with an empty `choices` list; it
is Azure's content-filter notice and is relayed as-is (the injected usage chunk is still hidden).

## Not covered

- Anthropic, AWS Bedrock and Google: no keys available; adapters are tested against documented
  shapes only.
- OpenAI chat and embeddings with a key that allows them.

## Follow-ups

- **Traceability (corrected):** `calls.http_status` already records the provider's status per
  attempt; the first reading of this test missed it. Remaining gaps: it isn't in the call logs, the
  ledger's read model or the tests, and the column has no range check.
- **Catalog:** add verified OpenAI transcription prices (`gpt-transcribe`, `whisper-1`).
