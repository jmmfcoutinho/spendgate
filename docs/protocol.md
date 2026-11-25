# spendgate wire protocol

Status: implemented in v0.1. Provider shapes checked 2026-11-13: Anthropic error page (platform.claude.com/docs/en/api/errors) fetched; OpenAI reference pages returned HTTP 403 to the fetch tool, so OpenAI field lists below come from the allowlist of a private tool that predates spendgate, plus prior knowledge, and were re-checked against recorded fixtures. Admin API: [admin-openapi.yaml](./admin-openapi.yaml).

**Additions to the glossary error codes:** `invalid_request` (400), `unauthorized` (401), `forbidden` (403), `not_found` (404, admin only), `conflict` (409, admin only; on the data plane a ledger `conflict` on a gateway-generated request id maps to `internal_error`), `internal_error` (500). Nothing else is added.

## 1. Principles

1. Data plane = provider shapes. An unmodified OpenAI or Anthropic SDK works with only `base_url` and key changed. Nothing is returned as a custom body (speech returns audio bytes, not base64 JSON).
2. Allowlist, never denylist: unknown request fields are refused (`invalid_request`, `param` names the field). A new provider feature stays closed until added here.
3. The gateway rebuilds the upstream body from validated fields; it never forwards the raw client body, client `Authorization`, or any `X-Spendgate-*` header.
4. Fail closed: every refusal happens before upstream contact. Refused requests write no `calls` row (logs and metrics only). Once the request has been sent upstream, a failure or cancel is charged the full hold unless usage was seen (rule 4 of section 4.2).
5. Order: the endpoint allowlist (this section) is validated **before routing**; the provider adapter's `translate()` runs **after** routing and the hold (pipeline steps 3 and 8, architecture.md section 3). A request that passes the allowlist but cannot be translated for the chosen provider is `invalid_request` with `param`.

## 2. Data-plane endpoints

| Method + path | Shape | Streaming | Notes |
|---|---|---|---|
| `POST /v1/chat/completions` | OpenAI chat | SSE | Providers: `openai`, `azure_openai`, `bedrock`, `google` (translated, see 2.7) |
| `POST /v1/responses` | OpenAI Responses | SSE | `openai`, `azure_openai` only |
| `POST /v1/embeddings` | OpenAI | no | `openai`, `azure_openai` (Gemini embeddings deferred) |
| `POST /v1/audio/transcriptions` | OpenAI, multipart | no (v0.1) | `openai`, `azure_openai`, `azure_speech` |
| `POST /v1/audio/speech` | OpenAI, JSON in, audio bytes out | chunked audio | `elevenlabs`, `openai` |
| `GET /v1/models`, `GET /v1/models/{id}` | OpenAI list; Anthropic list if request has `anthropic-version` | no | Only models priced **and** allowed for the calling key |
| `POST /v1/messages` | Anthropic Messages | SSE | `anthropic` only (v0.1: no translation) |

No Realtime endpoint in v0.1 (see the [roadmap](../ROADMAP.md)). Provider slugs are adapter module names.

Other paths under `/v1/` return 404 `not_found`. Ops endpoints (no auth, no body): `GET /livez`, `GET /readyz` (ledger + catalog loaded, a kill switch never fails it; the status code and `{"status"}` only, details at `GET /admin/v1/readiness`), `GET /metrics` (Prometheus; served by the admin app only, see 6).

**`model` field.** A gateway model name from `spendgate.yaml` (`models:` alias, which maps to an ordered list of deployments of **one** catalog id, or the catalog id itself such as `openai/gpt-5-mini`). Fallback in v0.1 is between deployments of the same catalog id only (regions, resources, keys); cross-model fallback is on the roadmap. Not listed for the key: `model_not_allowed` (403). Listed but no current catalog price: `model_not_priced` (404). Both are checked before any upstream call. The name is rewritten to the provider's model or deployment name upstream; the response `model` field is returned as the provider sent it.

### 2.1 Common request rules

| Rule | Value |
|---|---|
| Body type | `application/json` (multipart only for transcriptions); else 415 `invalid_request` |
| Max body | JSON: `server.max_json_body_bytes` (default 4 MiB); transcriptions: `server.max_body_bytes` (default 25 MB file). The key is checked first, then the size from `Content-Length` and by a read cap before parsing: `request_too_large` 413. A body not fully received within `server.body_read_timeout_seconds` (default 30) is refused with 408 `invalid_request`. Body memory in flight is charged by the bytes actually buffered (times the copies alive: 32 for JSON, 3 for audio), refused with 429 when the process gate cannot take more, and one key may hold at most a quarter of it. Gate memory = `max(384 MiB, max_body_bytes x 3, max_json_body_bytes x 32)`; raising `max_json_body_bytes` above about 12 MiB needs a container larger than the reference 512 MiB |
| Output ceiling | Precedence: the request's `max_tokens` / `max_completion_tokens` / `max_output_tokens`, else the key's `limits.max_output_tokens` (or config `defaults.max_output_tokens`), else the catalog entry's `limits.max_output_tokens` (required for chat, responses, messages). If none exists the request is refused `invalid_request` (`param` = the field). The gateway resolves **one** ceiling, sizes the hold with it and always forwards exactly that ceiling in a single field: `max_completion_tokens` for chat on OpenAI and Azure OpenAI (a client `max_tokens` is not forwarded), `max_output_tokens` for responses, `max_tokens` for messages, `maxTokens` (Bedrock Converse) and `maxOutputTokens` (Gemini). When the client set none, the gateway **adds** the resolved ceiling upstream; the added value is not echoed. A client field never reaches the provider beside it, so a provider that prefers one spelling cannot run past the hold (M-15). The upstream must therefore honour that field: `max_completion_tokens` is the only chat ceiling sent to `openai` and `azure_openai` (reasoning models reject `max_tokens`), so an Azure deployment should use the v1 API (no `api_version`) or a classic `api_version` that accepts `max_completion_tokens` (the minimum such version is not verified here), and an OpenAI-compatible server behind `base_url` (vLLM, LiteLLM, ...) that ignores `max_completion_tokens` leaves the ceiling unenforced upstream: the hold stays as sized, and usage beyond it is booked as overrun. A request value above the smaller of the key and catalog ceilings is refused (`request_too_large`, `param` = field) |
| Currency | If any catalog currency differs from the ledger currency, config `fx: {rates, as_of, source}` is required; the gateway refuses to start without it. Each call records the rate used. In the library quick start the ledger currency follows the cap's currency (`guard(cap="5 USD")` works with the bundled USD catalog); a cap in another currency needs `fx=` and otherwise fails with a clear message |
| Remote media | Image/audio/file URLs that the provider would fetch are refused (SSRF, provider-side fetch). Only inline `data:` URLs. URLs in request bodies are refused (`invalid_request`) unless config `fail_open.allow_remote_media: true`, an explicit opt-in logged at start, which admits `http(s)` URLs in image positions only (their content type is not checked; the provider fetches and bills what they serve) |
| Inline media | Only images are priced, so inline media must be `data:image/png`, `image/jpeg`, `image/webp` or `image/gif` with `;base64,` (Anthropic: `source.media_type` one of those four; Gemini: png, jpeg or webp only), the payload base64 on one line. Video, audio, PDF or any other type is refused `invalid_request` naming the field, also with `allow_remote_media`. An OpenAI image `detail` must be `auto`, `low`, `high` or `original`, and Responses `instructions` must be a string. Only an image in a content position (a message's `content`, a Responses `function_call_output.output`, an Anthropic `tool_result.content`) is held as an image; an object labelled `"type": "image"` anywhere else (tool input, arguments) is counted as text. Each image part is held at the catalog entry's `limits.max_image_tokens`, else 61,000 input tokens, the largest per-image cost a provider documents plus 25 percent (M-14) |
| Hosted tools | Refused everywhere: web search, file search, code interpreter, MCP, computer use, connectors. Only caller-defined function tools |
| Server-side state | Refused everywhere: stored responses, conversations, background runs, assistants, files |

### 2.2 `POST /v1/chat/completions`

| Allowed | Notes |
|---|---|
| `model`, `messages` | Roles system, developer, user, assistant, tool. Content parts: `text`, `image_url` (image data URL only, see Inline media). `input_audio`, `file`: refused |
| `max_tokens`, `max_completion_tokens` | Ceiling rule above; both set: the smaller applies, and only it is forwarded (as `max_completion_tokens`) |
| `temperature`, `top_p`, `stop`, `seed`, `presence_penalty`, `frequency_penalty`, `logit_bias`, `logprobs`, `top_logprobs` | Passed through |
| `n` | Must be 1 (v0.1: cost estimation) |
| `response_format` | `text`, `json_object`, `json_schema` |
| `tools`, `tool_choice`, `parallel_tool_calls` | Function tools only (keys `type`, `function{name,description,parameters,strict}`) |
| `reasoning_effort`, `verbosity` | Passed through |
| `stream` | Boolean |
| `stream_options` | Only `{"include_usage": bool}`; see forced fields |
| `user` | Passed through; also attribution fallback (2.8) |
| **Refused** | `store` true, `metadata`, `modalities` other than `["text"]`, `audio`, `prediction`, `web_search_options`, `service_tier` (changes price), `functions`/`function_call` (deprecated), anything unknown |

**Forced:** `store=false` (a client `store=true` is refused); on `stream=true`, `stream_options.include_usage=true` is set. If the client did not ask for usage, the gateway **strips** the final usage-only chunk (empty `choices`) before it reaches the client; if the client asked, it passes through unchanged.

### 2.3 `POST /v1/responses`

| Allowed | Notes |
|---|---|
| `model`, `input`, `instructions` | `input`: string or items of type `message`, `function_call`, `function_call_output`, `reasoning`. Parts: `input_text`, `output_text`, `input_image` (image data URL only) |
| `max_output_tokens` | Ceiling rule |
| `text` (`format`, `verbosity`), `reasoning` (`effort`, `summary`, `generate_summary`) | Nothing else inside |
| `tools`, `tool_choice`, `parallel_tool_calls` | Function tools only; `tool_choice` none/auto/required/`{type:function,name}` |
| `temperature`, `top_p`, `truncation`, `stream`, `store` | `store` must be absent or false |
| `include` | Only `["reasoning.encrypted_content"]` (needed for stateless reasoning multi-turn) |
| `prompt_cache_key`, `user`, `safety_identifier` | Passed through |
| **Refused** | `previous_response_id`, `conversation`, `prompt`, `background`, `metadata`, `service_tier`, `stream_options`, unknown fields (today's guard ported and tightened) |

**Forced:** `store=false` (set when absent). The final `response.completed` event carries usage; nothing is stripped.

### 2.4 `POST /v1/embeddings`

Allowed: `model`, `input` (string, or array of strings/token arrays; max `defaults.max_embedding_items`, default 256), `encoding_format` (`float`, `base64`), `dimensions`, `user`. Others refused.

### 2.5 `POST /v1/audio/transcriptions`

`multipart/form-data`. Allowed: `file` (required; one part; size cap; container must be parseable so the gateway can **measure the duration itself**, which is what is priced: `invalid_request`, `param=file` if unmeasurable), `model`, `language` (ISO 639-1), `prompt`, `response_format` (`json`, `text`, `srt`, `verbose_json`, `vtt`), `temperature`, `timestamp_granularities[]`. Refused: `stream`, `chunking_strategy`, `include[]`, unknown parts. Response: provider shape and content type.

### 2.6 `POST /v1/audio/speech`

Allowed: `model`, `input` (max 4096 characters), `voice` (string; provider voice id validated by the adapter regex/allowlist), `instructions`, `response_format` (`mp3`, `opus`, `aac`, `flac`, `wav`, `pcm`), `speed`, `stream_format` (`audio` only; `sse` refused in v0.1). Response: `200` with raw audio bytes and the matching `Content-Type` (`audio/mpeg`, ...), chunked. ElevenLabs is reached through this same shape (`model` = `elevenlabs/eleven_multilingual_v2`, `voice` = provider voice id); OpenAI speech is the other provider. Billing is by characters, known before the first byte, so the cost header **is** sent (see 5).

### 2.7 `POST /v1/messages` and chat translation

`/v1/messages` allowed: `model`, `max_tokens` (required by Anthropic), `messages`, `system`, `temperature`, `top_p`, `top_k`, `stop_sequences`, `stream`, `tools` (custom function tools only), `tool_choice`, `thinking` (`enabled`/`adaptive` per model; budget counted in the hold), `output_config` (`effort`, `format`), `metadata.user_id` (attribution fallback). Content blocks: `text`, `image` (`base64` source of an image type only), `tool_use`, `tool_result`, `thinking`, `redacted_thinking`; `cache_control` allowed on blocks. Refused: server tools (`web_search`, `code_execution`, ...), `mcp_servers`, `container`, `service_tier`, `context_management`, top-level `cache_control` (automatic caching), `url`/`file` sources, unknown fields, and options the catalog does not price: `inference_geo` (US-only inference, 1.1×) and `speed` (fast mode). `temperature`, `top_p` and `top_k` are accepted only for versions before `2026-10-21`, which removed them (Anthropic answers 400). Headers `anthropic-version` and `anthropic-beta`: the client's version is passed through only if listed in `anthropic.allowed_versions` (default: `2023-06-01`, which older SDKs send, and the current `2026-10-21`; the gateway sends the current one when the client sends none), else `invalid_request` with `param: anthropic-version`; beta values must be on `anthropic.allowed_betas` (default empty). Inbound auth on this route accepts `Authorization: Bearer` (newer SDKs) and `x-api-key`.

Chat → Converse / Gemini translation covers only: `messages` (text, data-URL images, tool calls and results), `max_tokens`/`max_completion_tokens`, `temperature`, `top_p`, `stop`, function `tools` and `tool_choice`, `response_format` json_schema, `stream`, `user`. Any other allowed chat field sent to such a provider: `invalid_request` with `param` set (never silently dropped).

### 2.8 Streaming (SSE)

* `Content-Type: text/event-stream`, `Cache-Control: no-store`, `X-Spendgate-Request-Id` set at first byte. Event formats are the provider's: chat `data: {chunk}` ... `data: [DONE]`; Responses and Messages use named `event:` lines.
* **Pre-stream failure** (auth, validation, budget, routing, upstream error before the stream starts): normal JSON error with the HTTP status of section 4. An attempt for which `not_processed` holds (never sent, or a provider status in that provider's `free_statuses`: 429, plus 529 for `anthropic`) moves to the next candidate deployment of the same catalog id within the same reservation (cooldown applied, cost 0, at most `routing.max_attempts`, default 2), never streamed. A provider 5xx or timeout is returned **without** failover, charged the full hold, with `x-should-retry: false`.
* **Mid-stream failure** (upstream breaks, kill switch fires, session wall-clock limit, ledger lost): status is already 200, so one error event is written and the stream ends, shaped per endpoint: chat `data: {"error":{...}}` then `data: [DONE]`; Responses `event: error` + `{"type":"error","code","message","param","sequence_number"}`; Messages `event: error` + `{"type":"error","error":{...}}`. The call is settled from the stream's final (end-of-stream) usage if it already arrived, else charged in full (ledger rule). A running total from mid-stream, such as Gemini's per-chunk `usageMetadata` before the finish reason, is partial and never used.
* **Client disconnect:** the upstream call is cancelled. After the request was sent: final usage if seen, else the full hold (M-13); before it was sent: released at cost 0.
* Keep-alive: `: ping` comments every 15 s when idle (ignored by SDK parsers).
* The gateway never buffers a whole stream.

**Implementation notes (where the build settled details the design left open):**

* **Cost of a stream is an SSE comment, not a header.** After settlement, and before `data: [DONE]` for chat, the gateway writes `: spendgate request_id=... cost=... remaining=... scope=...` (`remaining` and `scope` only when the ledger could be read). A stream that broke, or whose settlement the sweeper made, carries no comment. The headers of a stream carry only `X-Spendgate-Request-Id` and the pre-call `X-Spendgate-Budget-Remaining` (section 5).
* **Mid-stream stops are checked by the heartbeat** (every `ledger.heartbeat_seconds`, default lease/3 = 20 s), not per chunk. A kill switch that covers the stream (global, the key's scope or an ancestor, the key, the model, or the serving provider) or a session whose `max_seconds` has passed since its first call ends the stream within one beat with the error event above; it settles as `stream_broken` (final usage if seen, else the full hold). Lease loss stops it the same way.
* **Concurrency.** The server gate (`server.max_concurrent_requests`) covers request intake only: it is held while the body is read and the request is admitted, and is released as soon as the reply (or the stream handle) exists. A running stream still counts against the key's `max_concurrent` limit until it settles.

## 3. Identity

| Header | Required | Meaning |
|---|---|---|
| `Authorization: Bearer sg-<prefix>-<secret>` | yes (or `x-api-key`) | Virtual key; bound to one scope. Looked up by `<prefix>`, verified against the stored hash |
| `x-api-key: sg-...` | alt | Same key, for the Anthropic SDK. If both are present they must match, else `unauthorized` |
| `X-Spendgate-Session` | no; yes on a `session_required` key | Agent session or run id. Charset `[A-Za-z0-9._:-]`, 1-128 chars |
| `X-Spendgate-User` | no | End-user id, same charset, 1-128. Attribution only |
| `X-Spendgate-Tags` | no | Comma list, each `[a-z0-9._:-]{1,32}`, at most 10. Attribution only |

* Invalid label values: `invalid_request` 400 with `param` = header name (never silently dropped, never truncated).
* **User and tags** are recorded on the call and usable in usage queries. They never affect a budget, limit or routing decision. Fallbacks when `X-Spendgate-User` is absent: body `user` (OpenAI) or `metadata.user_id` (Anthropic).
* **Session = a child scope** of the key's scope, identified by `(key id, session id)`, created on first use with the key's `session_defaults`; the session scope path includes the key id (segment `session:<key id>:<session id>`, e.g. `org:acme/project:app/service:api/session:01J...:nightly-import`), so sessions are **per key**: two keys on one scope never share a session (`budget_nano`, `max_steps`, `max_seconds`, `idle_seconds`, `max_new_sessions_per_hour`). Properties:
  * The header is accepted **only for keys that have `session_defaults`**; otherwise `invalid_request` with `param: X-Spendgate-Session`. Without the header there is no session and no session limit, so a key meant for an agent should set **`session_required: true`**: a call on it without the header is refused with `invalid_request` and `param: X-Spendgate-Session` (message `This key requires the X-Spendgate-Session header.`) before the rate limits are counted and before any hold (only the per-key intake slot is held while the body is read).
  * It can only add limits: the parent chain (org, project, service, user) is always checked too, and the tightest scope decides.
  * A client cannot choose a budget, raise a default, or reach another key's session: the id is namespaced by key id, and no header selects a budget or limit.
  * `max_steps` counts one step per client request (not per attempt), refunded only by a final `release`; `max_seconds` runs from the first call; `idle_seconds` ends an idle session: the old row is closed and renamed, the same id starts a fresh session (step counter restarts, parent budgets still apply). A session that hit any limit (budget, steps, time) stays closed: the same id keeps getting `session_limit_reached`; only an idle-expired session restarts. Residual (TH-09): up to `max_new_sessions_per_hour` x session budget per hour, always inside the parent budgets. `max_new_sessions_per_hour` stops an agent dodging limits by minting ids; it is 60 when a key's `session_defaults` leave it out (`null` removes the bound).
  * Any exhausted session limit returns `session_limit_reached`, including the session's **own** budget (`details.limit = "budget"`, never `budget_exhausted`); `details.limit` is machine-readable: `budget`, `steps`, `time`, `new_sessions`. Parent-chain budget refusals keep `budget_exhausted`. A refused call is not counted as a step.
* A call with an expired, revoked or unknown key: `unauthorized` 401. A valid key that may not use the model or endpoint: `model_not_allowed` / `forbidden` 403.
* Keys never appear in responses, logs or errors. Upstream receives only the gateway's provider credentials.

## 4. Error model

### 4.1 Body shapes

OpenAI-shaped endpoints (all `/v1/*` except `/v1/messages`):

```json
{"error": {"type": "budget_error", "code": "budget_exhausted", "message": "Budget for org:acme/project:app/service:chat is exhausted.", "param": null,
           "details": {"scope": "org:acme/project:app/service:chat", "remaining": "0.000000000 EUR", "hold": "0.003000000 EUR", "period_resets_at": "2026-12-01T00:00:00Z"}}}
```

Anthropic-shaped (`/v1/messages`), same codes plus the request id as the Anthropic API does:

```json
{"type": "error", "error": {"type": "billing_error", "code": "budget_exhausted", "message": "...", "details": {...}}, "request_id": "sg_req_..."}
```

* `code` is the stable contract: client logic switches on `code`, never on `message` or `type`. `message` is human text and may change; it contains no secrets or prompt text.
* `param` is the offending field or header name or `null`. `details` is optional and typed per code: `scope`, `remaining` (decimal string + currency), `hold` (the refused call's worst-case hold, same format as `remaining`; set when a budget had no room for the hold, not on `reason = overrun_review`), `limit` (`budget`/`steps`/`time`/`new_sessions`/`rpm`/`tpm`/`concurrency`), `reason` (`overrun_review` on `budget_exhausted`: a breach overrun awaits `POST /admin/v1/budgets/{id}/ack-overrun`), `period_resets_at` (RFC 3339 UTC), `retry_after_seconds`, `provider`.
* Every error response carries `X-Spendgate-Request-Id`. For non-retryable errors the gateway also sends `x-should-retry: false`, which the OpenAI and Anthropic SDKs honour, so their automatic retries do not repeat a refused call.

### 4.2 Code table

`type` values: OpenAI shape / Anthropic shape in brackets.

| Code | HTTP | `Retry-After` | `type` (OpenAI / Anthropic) | When |
|---|---|---|---|---|
| `invalid_request` | 400 | no | `invalid_request_error` / same | Malformed body, unknown or refused field or header value; `param` set |
| `unauthorized` | 401 | no | `authentication_error` / same | Missing, malformed, unknown, revoked or expired key |
| `budget_exhausted` | 402 | no | `budget_error` / `billing_error` | A budget with `period: none` (or any cap) has no room for the worst-case hold, or `strict_overrun` holds it for review (`details.reason = overrun_review`); `details.scope` is the tightest scope. With `details.remaining` above zero the budget is not spent: the message says the worst-case hold (`details.hold`) exceeds what remains and suggests setting or lowering `max_tokens` (or `max_output_tokens`) or raising the cap; with nothing left it says the budget is exhausted |
| `budget_period_exhausted` | 402 | yes: seconds to next UTC period start | `budget_error` / `billing_error` | Same (including the hold-aware message), for a `day` or `month` budget; `details.period_resets_at` set |
| `session_limit_reached` | 402 | no | `budget_error` / `billing_error` | The session's own budget, step limit, time limit or new-session rate reached, or a session already closed by one of them; `details.limit` says which |
| `forbidden` | 403 | no | `permission_error` / same | Key lacks the endpoint or admin scope |
| `model_not_allowed` | 403 | no | `permission_error` / same | Model not in the key's allow list or not in config |
| `model_not_priced` | 404 | no | `invalid_request_error` / `not_found_error` | In config but no current catalog price (or price expired); refused before upstream |
| `not_found` | 404 | no | `invalid_request_error` / `not_found_error` | Unknown path or admin object |
| `conflict` | 409 | no | `invalid_request_error` / `conflict_error` | Admin: duplicate or stale update |
| `request_too_large` | 413 | no | `invalid_request_error` / `request_too_large` | Body over limit, or requested output above the ceiling |
| `rate_limited` | 429 | yes (token-bucket refill / concurrency: 1-2 s) | `rate_limit_error` / same | **Our** RPM, TPM or concurrency limit for the key or scope (concurrency is 16 calls in flight per key unless configured); `details.limit` says which |
| `upstream_throttled` | 429 | yes (provider `Retry-After`, else cooldown remaining) | `rate_limit_error` / same | Every candidate (up to `routing.max_attempts`) answered with a `free_statuses` status (429; 529 for `anthropic`). `details.provider` set |
| `internal_error` | 500 | no | `server_error` / `api_error` | Unexpected gateway bug; fails closed. Before upstream contact: no charge. After upstream contact: the full hold is charged (settle as `internal_error`) |
| `upstream_unavailable` | 502 (upstream 5xx or connection error), 504 (upstream timeout), 503 (all deployments circuit-open) | 503 only: breaker cooldown remaining | `upstream_error` / `api_error` (`timeout_error` for 504) | Provider 5xx, timeout or connection error. 5xx and timeout are returned **without** failover and with `x-should-retry: false`; a connection failure before send fails over first (`not_processed`), and 502 follows only when no candidate is left; a model with a single deployment retries a connect failure once on that same deployment (within `routing.max_attempts`) |
| `kill_switch` | 503 | no | `server_error` / `api_error` | A kill switch (global, scope, key, provider or model) applies; stays until an admin lifts it. Provider kills refuse only when every candidate deployment is killed |
| `ledger_unavailable` | 503 | yes, 5 s | `server_error` / `api_error` | Ledger unreachable; fail closed, no upstream contact |

Rules:

1. **A provider 429 or 529 never becomes `budget_exhausted`.** Budget codes come only from our ledger; provider throttling is `upstream_throttled` (429, retry later); provider quota or billing errors (provider 402/403 on our key) are `upstream_unavailable`, with the provider named in `details.provider` and the provider message withheld from the client.
2. 429 is used for both `rate_limited` and `upstream_throttled` because SDK retry logic for rate limits is correct for both; clients tell them apart by `code`. Budget refusals use 402 (not retried by SDKs; matches Anthropic's `billing_error`).
3. Retry guidance for clients: retry `rate_limited`, `upstream_throttled`, `ledger_unavailable` using `Retry-After` when present; `upstream_unavailable` after a 5xx or timeout is the caller's decision (the gateway sets `x-should-retry: false` because the hold was charged); never retry the others unchanged. `budget_period_exhausted` becomes retryable at `Retry-After`.
4. Charging (M-13): refusals cost nothing and write no `calls` row. An attempt for which `not_processed` holds (never sent, or a `free_statuses` answer) costs nothing. A provider 4xx without usage (a rejected request, not throttling) is settled at 0 with no failover. After the request was sent, any other failure or cancel (5xx, timeout, stream cut, client disconnect, `internal_error` after upstream contact) is charged the **full hold** unless usage was seen (for a stream: its final, end-of-stream usage); the ledger rule decides and `X-Spendgate-Cost` reflects it. For 5xx and timeout the response sets `x-should-retry: false`.
5. Errors for refused requests never reveal other scopes' budgets: `details.scope`, `remaining` and `hold` are shown only for scopes in the caller's own chain.

## 5. Response headers

| Header | On | Value |
|---|---|---|
| `X-Spendgate-Request-Id` | every response, including errors and the SSE handshake | `sg_req_<ulid>`; unique reservation id in the ledger; quote it in support |
| `X-Spendgate-Cost` | non-stream success; speech with per-character pricing; errors where something was charged | Settled cost, decimal string + ledger currency, 9 decimals max: `0.001234567 EUR`. `0.000000000 EUR` for a refusal |
| `X-Spendgate-Budget-Remaining` | success and budget/session errors | Remaining room in the **tightest** scope after settlement (cap minus spent minus held): `12.500000000 EUR`. Omitted when no budget applies |
| `X-Spendgate-Budget-Scope` | with the line above | Human path of that tightest scope, e.g. `org:acme/project:app/service:chat` |

**Streams: not a header.** Headers are sent before generation, so the settled cost does not exist yet; HTTP trailers are not exposed by the OpenAI or Anthropic SDKs or by most proxies. Decision: the gateway sends `X-Spendgate-Request-Id` and a **pre-call** `X-Spendgate-Budget-Remaining` (remaining after the hold, a conservative lower bound) in the headers, and after settlement, as the last bytes before the stream closes (before `data: [DONE]` for chat), one SSE comment:

```text
: spendgate request_id=sg_req_01J... cost=0.001234567 EUR remaining=12.498765433 EUR scope=org:acme/project:app/service:chat
```

Justification: SSE comments are ignored by spec-compliant parsers (including both SDKs), so nothing breaks; it needs no custom event type; a client that wants it reads raw lines. Authoritative cost is always available afterwards from `GET /admin/v1/calls/{request_id}` (`usage:read`). Streams that break before settlement carry no comment; the ledger still charges them. Retrofitting the comment into an unused event type is rejected because OpenAI/Anthropic SDK stream parsers could fail on unknown JSON events.

Other headers: `Retry-After` per 4.2; `x-should-retry` (above). Provider headers (`openai-*`, `anthropic-ratelimit-*`, `x-request-id`) are dropped except `Content-Type`, `Content-Length`/`Transfer-Encoding`, `Cache-Control` and `Retry-After`.

## 6. Admin API

* Base `/admin/v1`; JSON; spec in [admin-openapi.yaml](./admin-openapi.yaml) (OpenAPI 3.1).
* **Admin keys** are a separate key type, `sga-<prefix>-<secret>`, never accepted on the data plane (and `sg-` keys never on admin). Scopes: `usage:read` (usage, calls, audit, status), `keys:write` (keys and scopes), `budgets:write` (budgets, grants, `ack-overrun`, kill switches). Read access to keys, scopes and budgets needs `usage:read`. Admin keys are created and rotated by `spendgate admin-key create` on the host (bootstrap), not through the API.
* **`/metrics` is served only by the admin app.** On a separate admin listener it is open to whoever can reach that port (the port is the perimeter); on a shared port it needs an admin key with `usage:read`. The data-plane app never serves it.
* Optional separate listener: `server.admin.listen: 127.0.0.1:8081`. If unset, the admin API shares the data-plane port; if set, `/admin/v1/*` on the data port returns 404.
* Errors use the OpenAI shape and codes of 4.2 (`unauthorized`, `forbidden`, `invalid_request`, `not_found`, `conflict`, `ledger_unavailable`). Every write is stored in `admin_audit` (who, what, before/after, request id).
* Scope ids are ULIDs; every endpoint taking a scope also accepts its unique human path (`org:acme/project:app/service:api`). Kill-switch targets: `global`, `scope`, `key`, `provider`, `model`.
* Money: `{"amount": "12.500000000", "currency": "EUR"}` (decimal string, up to 9 decimals, caps non-negative; stored as integer nano-units, never floats). Per-attempt `Call` records are an audit view and expose raw `_nano` integers. Times RFC 3339 UTC. Pagination: `limit` (default 50, max 500) and opaque `cursor`; responses `{"data": [...], "next_cursor": null|"..."}`.
* No `Idempotency-Key` in v0.1 (it needs a store); clients check before retrying a POST.

## 7. Versioning and compatibility

| Surface | Rule |
|---|---|
| Data plane | Follows OpenAI and Anthropic shapes; no spendgate version prefix. New provider fields stay refused until a spendgate minor release adds them. Allowlist additions are minor releases; removals are major |
| Error `code` list | Stable within a major version; new codes only in minor releases, and clients must treat unknown codes by HTTP status |
| `X-Spendgate-*` headers | Additive within a major version |
| Admin API | `/admin/v1`; breaking changes go to `/admin/v2`, with v1 kept for one minor series |
| Provider-specific behaviour | Documented per adapter; fixtures dated |

## 8. Integration quick reference

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8080/v1", api_key="sg-ab12-...",
                default_headers={"X-Spendgate-Session": "nightly-import", "X-Spendgate-User": "u-42",
                                 "X-Spendgate-Tags": "import,batch"})
# Anthropic: Anthropic(base_url="http://localhost:8080", api_key="sg-ab12-...")  # sent as x-api-key
# An agent's key should have `session_required: true`: every call then must name its session.
```

Handle in order: `402` (stop, tell the user; `code` says why), `429` (back off by `Retry-After`; if `code == "upstream_throttled"` the provider, not your budget, is the limit), `5xx` (the gateway sets `x-should-retry: false` on provider 5xx and timeouts because the hold was charged; the caller decides whether to retry; `kill_switch` will not recover on its own).

## 9. Questions

Closed questions:

1. Session-id minting and omission: all three guards apply. `X-Spendgate-Session` is accepted only for keys with `session_defaults`, `max_new_sessions_per_hour` (60 by default) bounds minting, and a `session_required` key refuses calls that omit the header. Closed.
2. 429 for `upstream_throttled` (chosen) vs 503: 429 gets SDK retry for free; clients tell the two apart by `code`. Closed.
3. `X-Spendgate-Budget-Scope` is kept as its own header (value: the human path of the tightest scope). Closed.

Documented per adapter: 4. The Chat-to-Gemini/Converse translation field list (2.7), with dated fixtures.
