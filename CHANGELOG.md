# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
