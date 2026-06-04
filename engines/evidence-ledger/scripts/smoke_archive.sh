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
"$SPINE" doctor --mcp --json >"$TMP_WORK/doctor.json"
"$SPINE" import adapter "$ROOT/testdata/adapters/discrawl.fixture.jsonl" --source discrawl --json >"$TMP_WORK/import-discrawl.json"
"$SPINE" import codex "$ROOT/testdata/harnesses/codex-session.fixture.jsonl" --json >"$TMP_WORK/import-codex.json"
"$SPINE" import openclaw "$ROOT/testdata/harnesses/openclaw-session.fixture.jsonl" --json >"$TMP_WORK/import-openclaw.json"
"$SPINE" import claude "$ROOT/testdata/harnesses/claude-project.fixture.jsonl" --json >"$TMP_WORK/import-claude.json"
"$SPINE" import hermes "$ROOT/testdata/harnesses/session_hermes-demo.fixture.json" --json >"$TMP_WORK/import-hermes-snapshot.json"
"$SPINE" import hermes "$ROOT/testdata/harnesses/hermes-trajectory.fixture.jsonl" --json >"$TMP_WORK/import-hermes-trajectory.json"
"$SPINE" relations backfill --json >"$TMP_WORK/relations.json"
"$SPINE" stats --json >"$TMP_WORK/stats.json"
"$SPINE" compact --json >"$TMP_WORK/compact.json"
"$SPINE" search "Hermes snapshots" --source hermes --json >"$TMP_WORK/search-hermes.json"
"$SPINE" evidence "Hermes snapshots" --source hermes --json >"$TMP_WORK/evidence-hermes.json"

python3 - "$TMP_WORK" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])

def load(name):
    return json.loads((root / name).read_text())

doctor = load("doctor.json")
assert doctor["ok"] is True, doctor

stats = load("stats.json")
totals = stats["totals"]
assert totals["items"] >= 20, stats
assert totals["sources"] >= 5, stats
assert totals["unresolved_relations"] == 0, stats

compact = load("compact.json")
assert compact["ok"] is True, compact
assert compact["after_size_bytes"] > 0, compact

search = load("search-hermes.json")
assert len(search["results"]) >= 1, search

evidence = load("evidence-hermes.json")
assert evidence["untrusted_context"] is True, evidence
assert len(evidence["results"]) >= 1, evidence
assert isinstance(evidence["results"][0]["artifacts"], list), evidence

print("archive smoke ok")
PY
