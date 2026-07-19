#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

mapfile -t tools < <(
  sed -n '/^func mcpTools()/,/^func callMCPTool(/s/.*"name":[[:space:]]*"\([^"]*\)".*/\1/p' internal/app/mcp.go
)

if ((${#tools[@]} == 0)); then
  echo "docs drift: no MCP tools found in internal/app/mcp.go"
  exit 1
fi

status=0
for tool in "${tools[@]}"; do
  for doc in docs/EXAMPLES.md docs/MCP.md README.md; do
    if ! grep -Fq "\`$tool\`" "$doc"; then
      echo "docs drift: missing $tool in $doc"
      status=1
    fi
  done
done

if ((status != 0)); then
  exit "$status"
fi

echo "docs drift: ok"
