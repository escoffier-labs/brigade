#!/bin/sh
set -eu

repo="escoffier-labs/miseledger"
version="${MISELEDGER_VERSION:-latest}"
bindir="${BINDIR:-$HOME/.local/bin}"

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"
case "$arch" in
  x86_64|amd64) arch="amd64" ;;
  aarch64|arm64) arch="arm64" ;;
  *) echo "unsupported architecture: $arch" >&2; exit 1 ;;
esac

case "$os" in
  linux|darwin) ;;
  *) echo "unsupported OS: $os" >&2; exit 1 ;;
esac

asset="miseledger-${os}-${arch}"
session_asset="sessionfind-${os}-${arch}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if [ "$version" = "latest" ]; then
  url="https://github.com/${repo}/releases/latest/download/${asset}"
  session_url="https://github.com/${repo}/releases/latest/download/${session_asset}"
  sums_url="https://github.com/${repo}/releases/latest/download/checksums.txt"
else
  url="https://github.com/${repo}/releases/download/${version}/${asset}"
  session_url="https://github.com/${repo}/releases/download/${version}/${session_asset}"
  sums_url="https://github.com/${repo}/releases/download/${version}/checksums.txt"
fi

mkdir -p "$bindir"
curl -fsSL "$url" -o "$tmp/$asset"
curl -fsSL "$session_url" -o "$tmp/$session_asset"
curl -fsSL "$sums_url" -o "$tmp/checksums.txt"
(cd "$tmp" && grep " ${asset}$" checksums.txt | sha256sum -c -)
(cd "$tmp" && grep " ${session_asset}$" checksums.txt | sha256sum -c -)
install -m 0755 "$tmp/$asset" "$bindir/miseledger"
install -m 0755 "$tmp/$session_asset" "$bindir/sessionfind"
"$bindir/miseledger" version
"$bindir/sessionfind" version
