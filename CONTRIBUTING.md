# Contributing to spendgate

How spendgate code is written, reviewed and released. Everything here is enforced by a
command: a rule without a command is a wish. The targets are defined in this file and in the
`Makefile`.

## 1. Code style

- Readable over clever. If a reviewer has to pause, rewrite it.
- Type hints everywhere (`mypy --strict`). No `Any` in `core/` and `ledger/`.
- Docstrings for the public API only (one summary line, plus a paragraph when behaviour is non-obvious). Internal code gets a short comment where the reason is not obvious.
- Comments explain why, never what.
- Size guidelines: module <= ~400 lines, function <= ~50 lines, provider adapter <= ~400 lines, no limit on total product lines (ADR-0007; the total is reported). The lightweight report (section 3) flags outliers. Exceeding a guideline needs a one-line justification in the PR.
- No module-level mutable state. Dependencies are passed by constructor; time, randomness and I/O are injected (`Clock`, `Ledger`, `Provider` ports). Globals are constants only.
- Money is an integer in **nano**-units (fields end in `_nano`; `Decimal` only at the edge); never `float`.
- Fail closed: an unknown, unpriced or unreachable condition refuses the call. Any fail-open option is explicit, named and logged at start.
- Dependency direction is `cli > gateway | local > pipeline > ledger, providers, config, client > core` ([architecture.md](docs/architecture.md) section 4). No provider SDK in the base install; SDKs are optional extras.
- No telemetry, no remote price fetches, no hidden defaults.
- Plain SQL with numbered migration files; no ORM.

## 2. Tests

Pyramid, from most to fewest:

| Layer | Location | What it proves |
|---|---|---|
| Unit | `tests/unit/` | Pure `core/`: money, catalog, cost, estimation, policy, limits, settlement; plus `test_threat_model.py` (threat-model.md rows have control and test ids) and `test_dependencies.py` (base install has at most 6 runtime dependencies, 5 used) |
| Contract | `tests/contract/` | Every `Ledger` implementation (SQLite, Postgres) and every `Provider` adapter passes the same suite against recorded fixtures |
| Integration | `tests/integration/` | Gateway + fake provider + SQLite (and Postgres in CI) end to end |
| Load | `tests/load/` | No overshoot (8 processes x 50 calls), overhead p50/p99 |
| Perimeter | `tests/perimeter/` | Compose perimeter: no key outside the gateway, apps have no internet, apps reach the gateway |

Rules:

- A test name states the rule: `test_unpriced_model_is_refused_before_upstream_contact`. Every money rule has such a test.
- Hypothesis for money and the ledger (reserve/settle/release/expire/late-settle sequences never break the invariants: `spent + held <= effective_cap + overrun + excess`, no negative balances, idempotent settle).
- No network. Tests that need a real provider are marked `@pytest.mark.live`, excluded by default (`-m "not live"`) and never run in CI.
- Injected clock; no `sleep` in tests. Time moves by `clock.advance(...)`.
- A bug fix starts with a failing test.
- Coverage: >= 90% line on `core/`, `ledger/` and `pipeline/`; >= 95% on `core/`.
- Every money rule has an id `M-NN` in the money-rule table (section 7) and a test named for it.

## 3. Gates

All gates run locally and in CI with the same commands. None is added "later". CI is green on all of them or the PR does not merge.

| Gate | Command | Threshold |
|---|---|---|
| Lint | `uv run ruff check .` | zero findings |
| Format | `uv run ruff format --check .` | no diff |
| Types | `uv run mypy --strict src tests` | zero errors |
| Tests + coverage | `uv run pytest -m "not live" --cov=spendgate.core --cov=spendgate.ledger --cov=spendgate.pipeline --cov-fail-under=90` | >= 90% on `core/`, `ledger/` and `pipeline/` |
| Core coverage | `uv run pytest tests/unit --cov=spendgate.core --cov-fail-under=95` | >= 95% on `core/` |
| Architecture | `uv run lint-imports` | all contracts kept (core imports nothing; layers `cli` > `gateway` \| `local` > `pipeline` > leaf packages > `core`; `local` never imports `gateway`; no Starlette/uvicorn in `pipeline`; providers never import each other) |
| Dependencies | `uv run pip-audit` | no known vulnerabilities (documented, dated ignores only) |
| Secrets | `uv run detect-secrets-hook --baseline .secrets.baseline $(git ls-files)` | no new findings |
| Image scan | `docker build -t spendgate:ci . && trivy image --exit-code 1 --severity HIGH,CRITICAL spendgate:ci` | no HIGH/CRITICAL with a fix available |
| Lightweight report | `uv run python scripts/lightweight_report.py` | see below |

Coverage `fail_under` and the pre-commit hooks (ruff, ruff-format, mypy, detect-secrets) live in
`pyproject.toml` and `.pre-commit-config.yaml`; `uv run pre-commit run --all-files` runs the fast
gates locally. Python 3.13 and 3.14 both run the test gate.

### Lightweight report

`scripts/lightweight_report.py` (standard library only) prints and checks:

- product lines under `src/spendgate` excluding `providers/` (reported only, ADR-0007);
- lines per provider adapter (hard: <= 400 each);
- modules over 400 lines and functions over 50 lines (warnings; listed in the PR);
- runtime dependency count of the base install, from `[project].dependencies` (hard: <= 6).

Exit code 1 if a hard target is exceeded. Run it: `uv run python scripts/lightweight_report.py`.
Raising a limit needs an ADR.

## 4. Change management

- **Conventional Commits**: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `build:`, `ci:`, `chore:`; `!` or a `BREAKING CHANGE:` footer for breaking changes. Small commits in logical blocks.
- **Commit as yourself.** Commit messages, pull request titles and descriptions carry no assistant trailers, session links or generated-content markers, and a commit's author and committer are people, never a coding assistant's own git identity. `scripts/check-commit-message.sh` and `scripts/check-commit-identity.sh` enforce this in CI and as commit-msg hooks.
- **Pull requests** use `.github/pull_request_template.md`: what and why; the rule or target affected; tests added (named for the rule); gate output (including the lightweight report delta); ADR link if public behaviour changed; checklist (docs, CHANGELOG, no new runtime dependency without justification).
- **ADR** (`docs/adr/NNNN-title.md`) for any public behaviour change: config format, error codes, endpoints, ledger schema, default policy, a new runtime dependency, a raised size limit.
- **SemVer**: 0.x while the API settles (minor = may break, patch = fixes); 1.0 freezes the SDK, config, error codes and migrations. Migrations are append-only.
- **CHANGELOG.md** in Keep a Changelog format (`Added / Changed / Deprecated / Removed / Fixed / Security`), updated in the PR, not at release time.
- Review: one approval, plus a security-focused review for anything touching `core/`, `ledger/`, auth, key handling or the perimeter.

## Issues and pull requests

Report problems and ideas with the issue forms, and follow [docs/issues-and-prs.md](docs/issues-and-prs.md):
no secrets or personal data anywhere, security problems through a private advisory, one issue per branch.

## 5. Security process

See [SECURITY.md](SECURITY.md): supported versions, private reporting, response targets and scope.
Security is a gate on every change: perimeter and block tests run in CI; `pip-audit`, detect-secrets
and the image scan run on every PR and weekly on `main`.

## 6. Supply chain

- GitHub Actions pinned by full commit SHA (with a `# v4.x.y` comment); updates via Dependabot.
- Workflow `permissions: {}` by default; each job grants only what it needs (`contents: read`; `id-token: write` only in the release job; `packages: write` only for GHCR push). No `pull_request_target` with untrusted code; no long-lived secrets.
- Tools pinned by exact version in `uv.lock` (dev tools in the `dev` dependency group); CI installs with `uv sync --frozen`.
- Base images pinned by digest (`python:3.13-slim@sha256:...`), refreshed by Dependabot; final image is non-root, read-only root filesystem, no shell tools beyond need.
- Release: PyPI through trusted publishing (OIDC, no token); GHCR image with SBOM and keyless signature (cosign); built in CI from a tagged commit; tag only after the independent code and security reviews pass.
- Base install stays at <= 6 runtime dependencies; each addition needs an ADR entry naming why and what was considered.
- Releases are reproducible from the tag: `uv build` and the Dockerfile use pinned inputs only.

## 7. Money-rule table

Feeds `docs/how-money-is-protected.md`. Each rule gets an id, one sentence, the test that proves it and the doc section it comes from. Rows below are the rules; the tests are named for them.

| Id | Rule | Test name (to be written) | Source |
|---|---|---|---|
| M-01 | One reservation per client request, pending across attempts; hold = max over candidate deployments | | ledger-state-machine.md, scenario 4 (T-MON-02) |
| M-02 | A hold on an existing request id raises `conflict`; `release` is final and only when `not_processed` holds for every attempt | | scenarios 11 (T-MON-01) |
| M-03 | Reported usage is always booked, never clamped; the sanity cap only flags | | scenario 8, threat-model C-22 |
| M-04 | A late settle after `expired` books `max(0, charge - held)` as overrun and updates the sweeper's row | | scenario 13 (T-MON-03) |
| M-05 | Owner and sweeper race on one hold: exactly one money transition | | scenario 12 (T-MON-10) |
| M-06 | Effective cap = cap + active grants; expiry records `excess` lazily; `spent + held <= effective_cap + overrun + excess` | | scenario 10 (T-MON-04) |
| M-07 | An overrun inside the cap never blocks; a breach with `strict_overrun` blocks until `ack-overrun` | | scenarios 8, 15 (T-MON-05) |
| M-08 | Lowering then raising a cap keeps the invariant and recomputes `excess` | | scenarios 7, 16 (T-MON-06) |
| M-09 | A missing FX rate or a missing output ceiling is refused, never defaulted | | T-MON-07, T-MON-08 |
| M-10 | Catalog data cannot fail open (`charge_zero`, `free_statuses` outside 429/529 rejected) | | T-MON-09 |
| M-11 | Session steps count per client request; idle expiry restarts a session; sessions need `session_defaults` | | T-SESS-04..06 |
| M-12 | Ledger unavailable: no upstream contact | | T-BLK-06 |
| M-13 | Once the upstream request has been sent, a cancel or failure (client cancel, 5xx, timeout, stream cut) is charged the full hold unless usage was seen (for a stream, only its final end-of-stream usage counts, never a mid-stream running total; usage missing a required counter is unreadable, not 0); free only if `not_processed` (never sent, or a `free_statuses` answer such as 429) or the provider rejected it with a 4xx and no usage (settled at 0, no failover). Failover only after `not_processed` (never sent, or a `free_statuses` answer), at cost 0 | | scenarios 17-19 (T-MON-11) |
| M-14 | Images: inline media other than png, jpeg, webp or gif images is refused before routing; only parts in content positions count as images (an image label elsewhere is text); each image is held at the entry's `limits.max_image_tokens` or the conservative default (the largest documented per-image cost plus 25 percent). Text: without a tokenizer the input estimate is at or above the real count for natural and random text after the margin; chosen text can reach about 1.6x the margined estimate, never more than one token per UTF-8 byte (the excess is booked as overrun); tool definitions add their hidden prompt; the long-context tier is taken when the margined estimate reaches 80% of the threshold or the byte bound reaches it (proven for text, tool JSON and opaque inputs at their UTF-8 bytes; assumed for images at the catalog image ceiling and hidden prompts at the documented overheads) | `test_m14_input_estimate_is_an_upper_bound_without_a_tokenizer`, `test_m14_chosen_text_stays_within_the_documented_bound` and the other `test_m14_*` in `tests/unit/test_estimate_upper_bound.py`; the image `test_m14_*` in `tests/unit/test_estimate.py`, `tests/unit/test_catalog.py`, `tests/integration/test_inline_media.py` and `tests/contract/providers/test_ssrf_nested.py` | protocol.md 2.1 (Inline media), catalog-schema.md; heuristic.py, estimate.py, cost.py `worst_case` |
| M-15 | The upstream body carries exactly one output ceiling, equal to the one the hold was sized with; the client's ceiling fields are dropped and the resolved one is sent (added when the request had none) | `test_m15_the_forwarded_ceiling_equals_the_held_one` | protocol.md section 2.1 |
