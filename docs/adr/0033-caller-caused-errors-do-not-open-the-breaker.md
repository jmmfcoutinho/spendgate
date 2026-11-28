# ADR-0033: Errors a caller causes do not open the shared circuit breaker

**Status:** Accepted
**Date:** 2026-11-27

## Context

Each deployment has one circuit breaker (`core.health.DeploymentHealth`), shared by every alias,
key and org routed to it. When enough of its recent attempts fail, it opens for
`routing.cooldown_seconds` and every call routed only to it is refused with `503
upstream_unavailable` ("every deployment is temporarily unavailable").

A penetration test (SG-PT-03, recon H-06 and H-39) found that any key allowed one alias could open
it on purpose, for free:

- The pipeline counted every provider 401, 403 and 404 as a failure, on the grounds that they mean
  "our key, model or deployment is wrong, not busy". That holds for a 401, but a 403 or 404 can
  also answer one caller's request: a model the account may not use on this endpoint, a model that
  is not served on that path, a voice that does not exist.
- Nothing checked the endpoint against the routed catalog entry's `mode`, although the
  `RequestKind` docstring said they were equal. A chat alias sent to `/v1/embeddings` was forwarded
  with an input-only hold, and OpenAI answers it with 403 "You are not allowed to generate
  embeddings from this model", unbilled. On `/v1/audio/speech` it answers 404.

Four such calls (charged 0) opened the breaker in the lab; a key of another org then got 503 on
every call for the cooldown, and the attacker could repeat it every cooldown. With failover the
next deployment can be tripped the same way. No money is at risk; availability is. ElevenLabs had
the same hole without the mode mismatch: a 404 `voice_not_found`, for a voice the caller names,
also counted.

## Decision

- **A request goes only to an endpoint of its model's mode.** After the price lookup, before the
  output ceiling and the adapter's `validate`, the routed entry's `mode` must serve the endpoint's
  `RequestKind` (`core.modes.serves`). The text modes, chat, responses and messages, are one
  family: they say how an entry is billed, and each text adapter already refuses the text
  endpoints it cannot carry (a Bedrock `messages` model is served on `/v1/chat/completions`, an
  OpenAI `responses` model on both chat and Responses). Embedding, transcription and speech need
  an entry of their own mode. Anything else is refused with the existing `invalid_request` (400),
  `param` `model`, before any limit is counted, any hold is taken or anything is sent. No new code.
- **Only what fails for every caller counts towards the breaker.** A sent, answered error counts as
  a failure when it is a 5xx or 408 (as before), a 401 (only our credential is sent: a caller
  cannot cause it), or an answer the adapter marks `deployment_fault` on `UpstreamError`. Every
  other 4xx, a 403 or 404 included, counts as an answer, as a 400 always did.
- **Each adapter reads its provider's own codes.** `deployment_fault` is set only for a code that
  says our key, account, grants or configured model is wrong:

  | Adapter | Deployment faults |
  |---|---|
  | `openai` | `error.code` `invalid_api_key`, `model_not_found`, `unsupported_country_region_territory` |
  | `azure_openai` | the OpenAI codes, plus `DeploymentNotFound`, `PermissionDenied`, `AuthenticationTypeDisabled` |
  | `anthropic` | `error.type` `authentication_error`, `not_found_error` (the body names no other resource: only base64 images are accepted) |
  | `google` | any `API_KEY_*` reason (a bad key arrives as a 400), `PERMISSION_DENIED` with an `ErrorInfo` reason (`SERVICE_DISABLED`, `IAM_PERMISSION_DENIED`, ...), `UNAUTHENTICATED`, `NOT_FOUND` (media arrive inline, so only the model can be missing) |
  | `bedrock` | `x-amzn-ErrorType` `UnrecognizedClientException`, `InvalidSignatureException`, `IncompleteSignatureException`, `MissingAuthenticationTokenException`, `ExpiredTokenException`, `AccessDeniedException`, `ResourceNotFoundException` (the caller picks neither the model nor any other resource) |
  | `elevenlabs` | `detail.status` `invalid_api_key`, `missing_permissions` |
  | `azure_speech` | none: its bodies cannot tell our resource from the request; a 401 counts by status |

- **Nothing else changes.** The error code the client sees, the charge (a 4xx is still
  usage-or-zero), failover (only `not_processed` attempts fail over) and the cooldown rules of
  ADR-0021 stay as they were. An Anthropic `permission_error` is not counted: it can answer one
  request's feature as well as a key that lacks the model.

We considered counting 403 and 404 per (deployment, key) instead of per deployment. It needs
a second health table keyed by caller, and still lets an attacker with several keys open the
shared breaker; reading the provider's own code needs no new state and is tested per adapter.

## Consequences

- A chat alias on `/v1/embeddings` or `/v1/audio/speech`, an embedding or speech alias on chat or
  Responses, and the like, now get `400 invalid_request` with `param` `model` instead of being
  forwarded. Nothing correct is lost: the provider answered them with a 4xx, or (against an
  OpenAI-compatible `base_url` that serves both) billed them against an input-only hold (H-06).
- One key's 403s and 404s no longer refuse other keys' calls (T-DOS-05). A bad provider key, a
  missing Azure deployment or model, revoked AWS credentials or a disabled Google API still open
  the breaker, as a 5xx does, and the traffic moves to the next deployment of the alias.
- The trade-off: a misconfiguration that a provider reports with a 403 or 404 but no code we read
  (an Anthropic workspace without the model, an Azure Speech 403) no longer opens the breaker. Each
  call to it still fails at once, is charged 0 and is answered `502 upstream_unavailable` with
  `details.provider`, so it shows in `spendgate_requests_total` and in the `calls` rows
  (`http_status`, `error_code`); it is not silently served. Traffic does not move to the next
  deployment until the operator fixes it. A provider that changes its codes falls on the same
  side, never on the side of refusing other tenants.
- Default policy changes, so this ADR. No new config key, error code, endpoint, ledger change or
  dependency.
