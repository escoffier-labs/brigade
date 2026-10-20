#!/usr/bin/env bash
# Brigade plugin SessionStart hook: add a short work brief to the session.
# It must never block a session, so every path exits 0.
set -u

project="${CLAUDE_PROJECT_DIR:-$PWD}"
claude_home="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

# Brigade's enforced hooks already inject the brief; stay quiet when installed.
if [ -f "$claude_home/brigade/claude-hooks.json" ]; then
  exit 0
fi

root=""
probe="$project"
while [ -n "$probe" ] && [ "$probe" != "/" ] && [ "$probe" != "$HOME" ]; do
  if [ -f "$probe/.brigade/config.json" ]; then
    root="$probe"
    break
  fi
  probe="$(dirname "$probe")"
done

if [ -n "$root" ] && [ -f "$root/.brigade/claude-hooks.json" ]; then
  exit 0
fi

if [ -n "$root" ] && command -v brigade >/dev/null 2>&1; then
  if command -v timeout >/dev/null 2>&1; then
    timeout 5 brigade work brief --target "$root" 2>/dev/null | head -n 40
  else
    brigade work brief --target "$root" 2>/dev/null | head -n 40
  fi
  exit 0
fi

echo "Brigade memory is available through the brigade-memory MCP tools (search_cards, get_card, write_handoff). Memory outside a Brigade-wired repo lives in ${BRIGADE_HOME:-$HOME/.brigade}/memory."
exit 0
