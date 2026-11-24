# Instructions for coding assistants

For any coding assistant a contributor uses in this repository. Humans follow the same rules in
[CONTRIBUTING.md](CONTRIBUTING.md); where they differ, CONTRIBUTING.md wins.

## What this is

spendgate is a small, auditable LLM spend guard: a Python SDK plus a proxy that holds provider
keys and refuses any call that could break a budget. Start with
[docs/design-summary.md](docs/design-summary.md), then [docs/architecture.md](docs/architecture.md).
Planned work is in [ROADMAP.md](ROADMAP.md).

## Rules

- **Follow the design.** `docs/` is the spec. A change to public behaviour (config, error codes,
  endpoints, ledger schema, default policy, a new runtime dependency) needs an ADR in `docs/adr/`.
- **Respect the layers:** `cli > gateway | local > pipeline > ledger, providers, config, client > core`.
  `core` is pure: no I/O, no internal imports. `uv run lint-imports` enforces this.
- **Money is integer nano-units**, never `float`. Fail closed on anything unknown.
- **Keep it small.** Modules up to ~400 lines, functions up to ~50, at most 6 runtime
  dependencies. Readable over clever; comments explain why, not what.
- **No network in tests.** Inject the clock; never `sleep`. Mark real-provider tests `live`.
- **Every money rule has a test named for it** (rule ids `M-NN` in CONTRIBUTING.md section 7).
- **Run `make check` before declaring work done.** All gates must pass.
- **Never commit secrets,** real API keys or personal data. Use obviously fake values in tests.
- **Commits:** Conventional Commits, small logical blocks, authored and committed as the contributor,
  never under an assistant's own git identity (CI rejects it).

## Issues and pull requests

- Report problems and ideas as GitHub issues with the forms in `.github/ISSUE_TEMPLATE/` (bug, wrong or missing
  price, idea); follow `docs/issues-and-prs.md`.
- **Open issues only when the user asks.** Show the draft first unless the user already asked you to open it.
- Never put secrets (API keys, `sg-`/`sga-` keys, the pepper, database URLs), prompts, model outputs or personal
  data in issues, PRs, commits or tests. Use obviously fake values.
- A security problem (budget bypass, key leak, auth bypass) is never a public issue: tell the user and point to
  `SECURITY.md` (private advisory).
- One issue or small group per branch from `main`; the PR uses `.github/pull_request_template.md` and says
  "Closes #N"; merge only with CI green.
