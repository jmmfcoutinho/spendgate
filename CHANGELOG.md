# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `spendgate catalog import --from-litellm <commit-sha> --out extra.yaml [--providers ...]`
  drafts a catalog override file from LiteLLM's `model_prices_and_context_window.json` at one
  pinned commit (https to raw.githubusercontent.com only, no redirects), or from a local copy
  with `--from-file`. Every entry is `verified: false`; models the bundled catalog carries are
  left out, entries without an output ceiling are refused, and an existing file is kept unless
  `--force`. Nothing is fetched at serve time (ADR-0014). The mapping now lives in
  `spendgate.core.litellm` and `scripts/litellm_prices.py` uses it.
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

### Fixed

- `/readyz` no longer answers 503 while a global kill switch is engaged. The switch is shared, so
  every replica turned unready at once and load balancers dropped them all. Calls are still
  refused with `kill_switch` (503); the admin readiness detail lists the switch as a warning
  instead of a reason, and a failed read of it as the warning `kill_switch_probe_failed`
  (ADR-0015).

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
