# Issues and pull requests

How to report a problem or suggest an improvement, and how changes come back as pull requests.

## Secrets and privacy (always)

- Never paste API keys, virtual keys (`sg-…`, `sga-…`), the key pepper, database URLs or any other secret into an
  issue, a pull request, a commit, a test or a screenshot. Use obviously fake values in examples.
- Never paste prompts, model outputs or personal data from a deployment. Refer to calls by request id
  (`X-Spendgate-Request-Id`) and describe data in general terms.
- Keep logs short and remove anything secret or personal first.

## Security problems

A way to bypass a budget or a limit, reach a provider without a hold, leak a key or bypass auth is a
**vulnerability**: report it privately through a GitHub security advisory, never as a public issue. See
[SECURITY.md](../SECURITY.md).

## Opening an issue

Go to **Issues → New issue** and pick a form:

| Form | Use it for |
|---|---|
| **Bug report** | Something does not work: an error, a wrong charge, a hold that never settles, a refused call that should pass. |
| **Wrong or missing price** | A catalog price is wrong, stale or missing, or a model is retired. Give the provider's own page as the source. |
| **Idea or improvement** | A new feature, provider or deployment pattern, or a change to how something works. |

One problem per issue. Say what happened, what you expected, how to reproduce it, the version and the severity.

Labels: `bug`, `prices`, `enhancement` (set by the forms); add `money` when charging or budgets can be wrong,
`documentation` for docs-only issues.

Planned larger work is in the [roadmap](../ROADMAP.md); an item there becomes an issue when someone starts it.

## Pull requests

- One issue or one small group per branch, from `main`, named `<type>/<short-kebab-description>`
  (type: feat, fix, docs, chore, refactor, test, ci, perf, build, deps, prices, release); the PR says "Closes #N".
- Fill in the [pull request template](../.github/pull_request_template.md): what and why, the money rule or
  threat-model row affected, tests named for the rule, `make check` output, ADR link for public behaviour changes.
- CI must be green before merging. Behaviour that touches money needs a test that fails before the fix.
