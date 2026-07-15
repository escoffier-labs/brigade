#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MISELEDGER="${MISELEDGER:-$ROOT/bin/miseledger}"
QUERY="${MISELEDGER_GMAIL_SMOKE_QUERY:-subject:miseledger}"
LIMIT="${MISELEDGER_GMAIL_SMOKE_LIMIT:-1}"

if ! command -v gog >/dev/null 2>&1; then
  echo "gog not found on PATH" >&2
  exit 1
fi
if ! command -v mailcrawl >/dev/null 2>&1; then
  echo "mailcrawl not found on PATH" >&2
  exit 1
fi
if [ ! -x "$MISELEDGER" ]; then
  (cd "$ROOT" && go build -o bin/miseledger ./cmd/miseledger)
fi

account="$(gog auth list --json | python3 -c 'import json,sys; data=json.load(sys.stdin); accounts=data.get("accounts") or []; print(accounts[0].get("email","") if accounts else "")')"
if [ -z "$account" ]; then
  echo "no gog account configured" >&2
  exit 1
fi

"$MISELEDGER" crawl gmail \
  --account "$account" \
  --query "$QUERY" \
  --limit "$LIMIT" \
  --metadata-only \
  --dry-run \
  --json >/dev/null

echo "gmail metadata smoke ok"
