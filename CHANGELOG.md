# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- CI and the commit-msg hook also reject a commit whose author or committer is a coding
  assistant's own git identity (`scripts/check-commit-identity.sh`), next to the existing check on
  commit messages and pull request text.

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
