#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
TMP_HOME="$(mktemp -d)"
TMP_WORK="$(mktemp -d)"
trap 'kill "$server_pid" 2>/dev/null || true; rm -rf "$TMP_HOME" "$TMP_WORK"' EXIT

export HOME="$TMP_HOME"
export XDG_CONFIG_HOME="$TMP_HOME/.config"
export XDG_DATA_HOME="$TMP_HOME/.local/share"
export XDG_CACHE_HOME="$TMP_HOME/.cache"

MISELEDGER="${MISELEDGER:-$ROOT/bin/miseledger}"
if [ ! -x "$MISELEDGER" ]; then
  (cd "$ROOT" && go build -o bin/miseledger ./cmd/miseledger)
fi

"$MISELEDGER" init >/dev/null
"$MISELEDGER" import adapter "$ROOT/testdata/adapters/discrawl.fixture.jsonl" --source discrawl --json >/dev/null

addr="127.0.0.1:18765"
"$MISELEDGER" serve --addr "$addr" >"$TMP_WORK/server.log" 2>"$TMP_WORK/server.err" &
server_pid="$!"
sleep 1

curl -fsS "http://$addr/status" >"$TMP_WORK/status.json"
curl -fsS "http://$addr/search?q=adapter%20contract&source=discrawl" >"$TMP_WORK/search.json"
item_id="$(python3 - "$TMP_WORK/search.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
assert data["results"], data
print(data["results"][0]["id"])
PY
)"
curl -fsS "http://$addr/items/$item_id" >"$TMP_WORK/item.json"
curl -fsS -X POST "http://$addr/evidence" -d '{"query":"adapter contract","source":"discrawl","limit":5,"include_related":true}' >"$TMP_WORK/evidence.json"
python3 - "$TMP_WORK/evidence.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
assert data["untrusted_context"] is True, data
assert data["resource_uri"].startswith("miseledger://evidence/"), data
assert data["results"], data
print("http smoke ok")
PY
