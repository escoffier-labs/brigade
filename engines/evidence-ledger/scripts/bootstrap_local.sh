#!/bin/sh
set -eu

bindir="${BINDIR:-$HOME/.local/bin}"
mkdir -p "$bindir"

install_tool() {
  repo="$1"
  default_url="https://raw.githubusercontent.com/escoffier-labs/${repo}/HEAD/install.sh"
  case "$repo" in
    miseledger) url="${MISELEDGER_INSTALL_URL:-$default_url}" ;;
    stationtrail) url="${STATIONTRAIL_INSTALL_URL:-$default_url}" ;;
    sourceharvest) url="${SOURCEHARVEST_INSTALL_URL:-$default_url}" ;;
    *) echo "unknown repo: $repo" >&2; exit 1 ;;
  esac
  echo "installing ${repo} into ${bindir}" >&2
  curl -fsSL "$url" | BINDIR="$bindir" sh >&2
}

install_tool miseledger
install_tool stationtrail
install_tool sourceharvest

PATH="$bindir:$PATH"

miseledger init >/dev/null
miseledger doctor --mcp --json

echo "bootstrap ok: installed miseledger, stationtrail, and sourceharvest" >&2
echo "no private session content was imported" >&2
