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

SPINE="${SPINE:-$ROOT/bin/spine}"
if [ ! -x "$SPINE" ]; then
  (cd "$ROOT" && go build -o bin/spine ./cmd/spine)
fi

"$SPINE" init >/dev/null
"$SPINE" import adapter "$ROOT/testdata/adapters/discrawl.fixture.jsonl" --source discrawl --json >/dev/null

python3 - "$SPINE" >"$TMP_WORK/mcp.out" <<'PY'
import json, subprocess, sys

spine = sys.argv[1]
proc = subprocess.Popen([spine, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

def send(obj):
    payload = json.dumps(obj).encode()
    proc.stdin.write(b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload)
    proc.stdin.flush()

def recv():
    headers = {}
    while True:
        line = proc.stdout.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    length = int(headers["content-length"])
    return json.loads(proc.stdout.read(length))

send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}})
assert recv()["result"]["serverInfo"]["name"] == "logspine"
send({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})
tools = recv()["result"]["tools"]
assert any(t["name"] == "create_evidence_bundle" for t in tools), tools
send({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"create_evidence_bundle","arguments":{"query":"adapter contract","source":"discrawl","limit":5,"include_related":True}}})
resp = recv()
assert "untrusted_context" in resp["result"]["content"][0]["text"], resp
proc.stdin.close()
proc.terminate()
print("mcp smoke ok")
PY

cat "$TMP_WORK/mcp.out"
