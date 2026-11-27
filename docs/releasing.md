# Releasing

How the maintainer ships a release. Contributors do not need this page; [CONTRIBUTING.md](../CONTRIBUTING.md)
covers changes and pull requests.

## Normal release

1. **Release commit.** A pull request that bumps `version` in `pyproject.toml`, runs `uv lock` (the
   lock records the project version) and turns `## [Unreleased]` in `CHANGELOG.md` into
   `## [X.Y.Z] - YYYY-MM-DD` with a fresh empty `## [Unreleased]` above it. Merge it once CI is green.
2. **Tag, do not draft a release.** Push the tag on the merged commit:
   `git tag vX.Y.Z <sha> && git push origin vX.Y.Z` (the tag ruleset lets only the maintainer create
   `v*` tags). Do not create the GitHub Release by hand: the release workflow creates it from the
   CHANGELOG section once everything is published, and a Release that already exists makes that last
   step fail.
3. **Approve the `release` environment** twice in the workflow run: the image job and the PyPI job
   each wait for it. The `check` job first verifies that the tag matches the project version and that
   the CHANGELOG has a non-empty section for it.
4. **Check** that the version is on PyPI and the image on GHCR, and that the GitHub Release exists.

## Price updates

The daily price watch ([catalog-how-to.md](catalog-how-to.md#12-how-prices-stay-current)) opens one
pull request from `prices/daily-watch`, authored by `github-actions[bot]`.

1. **Review it** against the providers' pages, as that section says. Its CI run is a
   `workflow_dispatch` run that the price-watch workflow starts on the branch; it reports the
   required `CI gate` check on the head commit. If it is missing (the dispatch failed), start it:
   `gh workflow run ci.yml --ref prices/daily-watch`.
2. **Squash-merge** it once `CI gate` is green and you have approved it. Its commit is unsigned; the
   squash commit GitHub makes on `main` is signed, which is what the `main` ruleset requires. The
   pull request does not touch `CHANGELOG.md`, so that daily updates never conflict.
3. **Ship it in a patch release** (a normal release, `X.Y.Z+1`): the release commit adds a `Changed`
   line to the CHANGELOG naming the price pull request and the ids whose prices changed. A merged
   price change waits for no other work: until it is released, installs keep the old prices.

## Security release

A fix for a vulnerability (see [SECURITY.md](../SECURITY.md)) never appears in a public issue, pull
request, branch or commit message before the advisory is published.

1. **Draft the advisory** on the Security tab (New draft security advisory): title, summary, details,
   proof of concept, impact, affected and patched versions, CVSS and CWEs. Check every statement
   against the code of the affected version before saving.
2. **Build the fix locally**, on top of the current `main`, with the release commit (step 1 of a normal
   release) as its last commit. Run every gate locally (`make -k check`, the Postgres suite and
   `make load`): GitHub Actions do not run in the temporary private fork. Rebase onto the current
   `main` right before pushing; GitHub merges without any local merge driver, so a stale branch can
   conflict in `CHANGELOG.md` on the advisory page.
3. **Start a temporary private fork** from the advisory page, push the branch to it and open a pull
   request there against `main` (a normal one, not a draft).
4. **Merge from the advisory page.** The pull request page only says that merging is done from the
   advisory. On the advisory page, leave the repository rules as they are: tick **Merge and bypass
   branch protections** and click **Merge pull request**. The required `CI gate` check never reports
   in a private fork, and the bypass is how a repository admin merges anyway.
5. **Wait for CI on `main`** to pass on the merged result, then tag and approve as in a normal release.
6. **Publish the advisory** once the patched version is on PyPI. Requesting a CVE is optional and
   helps scanners other than Dependabot match the advisory.

## Merging a stack of pull requests

When several pull requests build on each other (each one's base is the previous one's branch), merge
them in order with a merge commit. A squash merge rewrites the merged commits, so every later pull
request in the stack then conflicts and has to be rebased and re-run before it can merge.

- **The bypass is needed.** The `main` ruleset allows only squash merges and requires a code owner's
  review, so the maintainer merges with the admin bypass: `gh pr merge <n> --merge --admin
  --match-head-commit <full head sha>`, one pull request at a time, in order.
- **Check the base first.** Deleting the previous branch after its merge retargets the next pull
  request to `main` automatically; wait until it shows `main` and GitHub has computed whether it can
  merge.
- **A retarget does not re-run CI.** The `pull_request` trigger does not fire on a base change, so
  the green run on the pull request is the one made against the old base. It still covers what lands
  when merging the head into `main` yields exactly the head's tree:
  `git merge-tree --write-tree origin/main <head>` equals `git rev-parse <head>^{tree}`. If it does
  not (`main` moved, or a conflict), merge `main` into the branch and let CI run again.
