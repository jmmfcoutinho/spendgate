#!/usr/bin/env bash
# Fails when the tracked tree contains assistant-tool configuration or markers.
# Run from anywhere; checks the files tracked by git in the repository root.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

status=0

# 1. Assistant-tool files and directories. AGENTS.md is deliberately kept: it is the neutral
#    instruction file for any coding assistant a contributor uses.
path_pattern='(^|/)(\.claude|\.codex|\.kilo|\.agents|\.cursor|\.windsurf|\.aider[^/]*|CLAUDE\.md|GEMINI\.md|copilot-instructions\.md)(/|$)'
if git ls-files | grep -E -- "$path_pattern"; then
  echo "assistant-tool files are tracked (listed above)" >&2
  status=1
fi

# 2. Markers in file contents: session links, trailers and tool names. The words Claude and
#    Anthropic are not matched on their own: they are a supported provider and model ids.
content_pattern='claude\.ai/|Claude-Session|Co-Authored-By:|Generated with \[|🤖|Claude Code|\bCodex\b|\bCopilot\b'

# Allowlist: files that contain the patterns as data, and generated or vendored data.
allow=(
  ':!scripts/check-commit-message.sh'
  ':!scripts/check-no-ai-traces.sh'
  ':!.pre-commit-config.yaml'
  ':!uv.lock'
  ':!tests/data/litellm/'
)
if git grep -n -I -E -- "$content_pattern" -- . "${allow[@]}"; then
  echo "assistant-tool markers found in tracked files (listed above)" >&2
  status=1
fi

[ "$status" -eq 0 ] && echo "no assistant traces in the tracked tree"
exit "$status"
