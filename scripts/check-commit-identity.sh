#!/usr/bin/env bash
# Rejects commit author and committer identities that belong to a coding assistant, so a commit
# made by a tool with its own git identity cannot land as if a person wrote it.
# Usage: git log -1 --format='%an <%ae>%n%cn <%ce>' <sha> | check-commit-identity.sh -
#    or: check-commit-identity.sh [<commit-msg file>]   (checks the identity git will use now;
#        the argument a commit-msg hook passes is ignored)
set -euo pipefail

if [ "${1:-}" = "-" ]; then
  idents="$(cat)"
else
  idents="$(git var GIT_AUTHOR_IDENT | sed 's/>.*/>/')"$'\n'"$(git var GIT_COMMITTER_IDENT | sed 's/>.*/>/')"
fi

# Whole words, any case, in the name or the address; and the assistant vendors' mail domains.
patterns=(
  '(^|[^[:alnum:]])(claude|copilot|codex|gemini|cursor|chatgpt)([^[:alnum:]]|$)'
  '@(anthropic|openai)\.com>'
)

status=0
while IFS= read -r ident; do
  [ -z "$ident" ] && continue
  for pattern in "${patterns[@]}"; do
    if printf '%s\n' "$ident" | grep -E -i -q -- "$pattern"; then
      echo "commit identity rejected: '$ident' matches /$pattern/" >&2
      status=1
    fi
  done
done <<< "$idents"
if [ "$status" -ne 0 ]; then
  echo "Commit as yourself: set user.name and user.email, then amend with --reset-author." >&2
fi
exit "$status"
