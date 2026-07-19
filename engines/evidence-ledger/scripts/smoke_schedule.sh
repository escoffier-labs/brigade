#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MISELEDGER="${MISELEDGER:-$ROOT/bin/miseledger}"

if [[ ! -x "$MISELEDGER" ]]; then
  (cd "$ROOT" && go build -o bin/miseledger ./cmd/miseledger)
fi

TMP_HOME="$(mktemp -d)"
TMP_WORK="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_HOME" "$TMP_WORK"
}
trap cleanup EXIT

export HOME="$TMP_HOME"
export XDG_CONFIG_HOME="$TMP_HOME/.config"
export XDG_DATA_HOME="$TMP_HOME/.local/share"
export XDG_CACHE_HOME="$TMP_HOME/.cache"

mkdir -p "$XDG_CONFIG_HOME/miseledger"
cat >"$XDG_CONFIG_HOME/miseledger/schedule.toml" <<EOF
interval = "1ms"

[[jobs]]
name = "discord-fixture"
command = "import"
args = ["adapter", "$ROOT/testdata/adapters/discrawl.fixture.jsonl", "--source", "discrawl", "--json"]
EOF

"$MISELEDGER" init >/dev/null
"$MISELEDGER" schedule run "$XDG_CONFIG_HOME/miseledger/schedule.toml" --json >"$TMP_WORK/schedule-run.json"
"$MISELEDGER" schedule daemon "$XDG_CONFIG_HOME/miseledger/schedule.toml" --interval 1ms --max-runs 2 --json >"$TMP_WORK/schedule-daemon.json"
"$MISELEDGER" status --json >"$TMP_WORK/status.json"

python3 - "$TMP_WORK" <<'PY'
import json
import pathlib
import sys

work = pathlib.Path(sys.argv[1])
run = json.loads((work / "schedule-run.json").read_text())
daemon = json.loads((work / "schedule-daemon.json").read_text())
status = json.loads((work / "status.json").read_text())

assert run["successful_jobs"] == 1, run
assert run["failed_jobs"] == 0, run
assert daemon["runs"] == 2, daemon
assert daemon["failed_runs"] == 0, daemon
assert status["items"] == 2, status
PY

echo "smoke_schedule ok"
