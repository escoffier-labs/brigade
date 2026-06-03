#!/bin/sh
set -eu

bindir="${BINDIR:-$HOME/.local/bin}"
mkdir -p "$bindir"

install_tool() {
  repo="$1"
  default_url="https://raw.githubusercontent.com/solomonneas/${repo}/HEAD/install.sh"
  case "$repo" in
    logspine) url="${LOGSPINE_INSTALL_URL:-$default_url}" ;;
    agenttrail) url="${AGENTTRAIL_INSTALL_URL:-$default_url}" ;;
    sourceharvest) url="${SOURCEHARVEST_INSTALL_URL:-$default_url}" ;;
    *) echo "unknown repo: $repo" >&2; exit 1 ;;
  esac
  echo "installing ${repo} into ${bindir}" >&2
  curl -fsSL "$url" | BINDIR="$bindir" sh >&2
}

install_tool logspine
install_tool agenttrail
install_tool sourceharvest

PATH="$bindir:$PATH"

spine init >/dev/null
spine doctor --mcp --json

echo "bootstrap ok: installed spine, agenttrail, and sourceharvest" >&2
echo "no private session content was imported" >&2
