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
them in order with **Create a merge commit**. A squash merge rewrites the merged commits, so every
later pull request in the stack then conflicts and has to be rebased and re-run before it can merge.
Before each merge, check that the pull request targets `main`; deleting the previous branch after its
merge retargets the next pull request automatically.
