#!/usr/bin/env bash
# Rejects commit messages (or PR titles and bodies) that carry assistant trailers, session links or
# generated-content markers. Usage: check-commit-message.sh <file>   (a commit-msg hook argument)
#        or:                        some-command | check-commit-message.sh -
set -euo pipefail

input="${1:--}"
if [ "$input" = "-" ]; then
  text="$(cat)"
else
  text="$(cat -- "$input")"
fi

patterns=(
  'Co-Authored-By:.*([Cc]laude|[Cc]opilot|[Cc]odex|[Gg]emini|[Cc]ursor|ChatGPT|\bAI\b)'
  'Claude-Session'
  'claude\.ai/'
  'Generated with \[Claude'
  '🤖'
)

status=0
for pattern in "${patterns[@]}"; do
  if printf '%s\n' "$text" | grep -E -q -- "$pattern"; then
    echo "commit message rejected: matches /$pattern/" >&2
    status=1
  fi
done
if [ "$status" -ne 0 ]; then
  echo "Remove assistant trailers, session links and generated-content markers." >&2
fi
exit "$status"
