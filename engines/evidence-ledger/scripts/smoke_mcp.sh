#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
TMP_HOME="$(mktemp -d)"
TMP_WORK="$(mktemp -d)"
trap 'rm -rf "$TMP_HOME" "$TMP_WORK"' EXIT

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

python3 - "$MISELEDGER" >"$TMP_WORK/mcp.out" <<'PY'
import json, subprocess, sys

miseledger = sys.argv[1]
proc = subprocess.Popen([miseledger, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

# Newline-delimited JSON-RPC: the MCP stdio spec, the framing Claude Desktop,
# the MCP Inspector, and Glama use. (The server also accepts Content-Length.)
def send(obj):
    proc.stdin.write(json.dumps(obj).encode() + b"\n")
    proc.stdin.flush()

def recv():
    return json.loads(proc.stdout.readline())

send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}})
assert recv()["result"]["serverInfo"]["name"] == "miseledger"
send({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})
tools = recv()["result"]["tools"]
assert any(t["name"] == "create_evidence_bundle" for t in tools), tools
assert any(t["name"] == "show_evidence_bundle" for t in tools), tools
send({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"create_evidence_bundle","arguments":{"query":"adapter contract","source":"discrawl","limit":5,"include_related":True}}})
resp = recv()
text = resp["result"]["content"][0]["text"]
assert "untrusted_context" in text, resp
bundle_id = json.loads(text)["id"]
send({"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"show_evidence_bundle","arguments":{"id":bundle_id}}})
shown = recv()
assert bundle_id in shown["result"]["content"][0]["text"], shown
proc.stdin.close()
proc.terminate()
print("mcp smoke ok")
PY

cat "$TMP_WORK/mcp.out"
