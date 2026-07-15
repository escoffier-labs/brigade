#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
TMP_HOME="$(mktemp -d)"
TMP_WORK="$(mktemp -d)"
cleanup() {
  chmod -R u+w "$TMP_HOME" "$TMP_WORK" 2>/dev/null || true
  rm -rf "$TMP_HOME" "$TMP_WORK"
}
trap cleanup EXIT

export HOME="$TMP_HOME"
export XDG_CONFIG_HOME="$TMP_HOME/.config"
export XDG_DATA_HOME="$TMP_HOME/.local/share"
export XDG_CACHE_HOME="$TMP_HOME/.cache"

MISELEDGER="${MISELEDGER:-$ROOT/bin/miseledger}"
if [ ! -x "$MISELEDGER" ]; then
  (cd "$ROOT" && go build -o bin/miseledger ./cmd/miseledger)
fi
SESSIONFIND="${SESSIONFIND:-$ROOT/bin/sessionfind}"
if [ ! -x "$SESSIONFIND" ]; then
  (cd "$ROOT" && go build -o bin/sessionfind ./cmd/sessionfind)
fi

"$MISELEDGER" init >/dev/null
"$MISELEDGER" doctor --mcp --json >"$TMP_WORK/doctor.json"
"$MISELEDGER" import adapter "$ROOT/testdata/adapters/discrawl.fixture.jsonl" --source discrawl --json >"$TMP_WORK/import-discrawl.json"
"$MISELEDGER" import codex "$ROOT/testdata/harnesses/codex-session.fixture.jsonl" --json >"$TMP_WORK/import-codex.json"
"$MISELEDGER" import openclaw "$ROOT/testdata/harnesses/openclaw-session.fixture.jsonl" --json >"$TMP_WORK/import-openclaw.json"
"$MISELEDGER" import claude "$ROOT/testdata/harnesses/claude-project.fixture.jsonl" --json >"$TMP_WORK/import-claude.json"
"$MISELEDGER" import hermes "$ROOT/testdata/harnesses/session_hermes-demo.fixture.json" --json >"$TMP_WORK/import-hermes-snapshot.json"
"$MISELEDGER" import hermes "$ROOT/testdata/harnesses/hermes-trajectory.fixture.jsonl" --json >"$TMP_WORK/import-hermes-trajectory.json"
"$MISELEDGER" import grok "$ROOT/testdata/harnesses/grok-sessions.fixture" --json >"$TMP_WORK/import-grok.json"
"$MISELEDGER" relations backfill --json >"$TMP_WORK/relations.json"
"$MISELEDGER" stats --json >"$TMP_WORK/stats.json"
"$MISELEDGER" compact --json >"$TMP_WORK/compact.json"
"$MISELEDGER" doctor --archive --json >"$TMP_WORK/doctor-archive.json"
"$MISELEDGER" prune imports --before 2000-01-01 --dry-run --json >"$TMP_WORK/prune-imports.json"
"$MISELEDGER" prune scans --missing --dry-run --json >"$TMP_WORK/prune-scans.json"
"$MISELEDGER" prune --policy default --dry-run --json >"$TMP_WORK/prune-policy.json"
"$MISELEDGER" search "Hermes snapshots" --source hermes --json >"$TMP_WORK/search-hermes.json"
"$MISELEDGER" search "fixture crawl contract" --source grok --json >"$TMP_WORK/search-grok.json"
"$SESSIONFIND" search "exec_command" --source codex --json >"$TMP_WORK/sessionfind-codex.json"
"$MISELEDGER" evidence "Hermes snapshots" --source hermes --json >"$TMP_WORK/evidence-hermes.json"
"$MISELEDGER" explain "Hermes snapshots" --source hermes --json >"$TMP_WORK/explain-hermes.json"
bundle_id="$(python3 - "$TMP_WORK/evidence-hermes.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["id"])
PY
)"
"$MISELEDGER" evidence show "$bundle_id" --json >"$TMP_WORK/evidence-show.json"

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
assert totals["sources"] >= 6, stats
assert totals["unresolved_relations"] == 0, stats

compact = load("compact.json")
assert compact["ok"] is True, compact
assert compact["after_size_bytes"] > 0, compact

doctor_archive = load("doctor-archive.json")
assert doctor_archive["ok"] is True, doctor_archive

assert load("prune-imports.json")["dry_run"] is True
assert load("prune-scans.json")["dry_run"] is True
assert load("prune-policy.json")["dry_run"] is True

search = load("search-hermes.json")
assert len(search["results"]) >= 1, search

grok_search = load("search-grok.json")
assert len(grok_search["results"]) >= 1, grok_search

sessionfind = load("sessionfind-codex.json")
assert len(sessionfind["sessions"]) >= 1, sessionfind
assert sessionfind["sessions"][0]["raw_path"], sessionfind

evidence = load("evidence-hermes.json")
assert evidence["untrusted_context"] is True, evidence
assert evidence["resource_uri"].startswith("miseledger://evidence/"), evidence
assert len(evidence["results"]) >= 1, evidence
assert isinstance(evidence["results"][0]["artifacts"], list), evidence

evidence_show = load("evidence-show.json")
assert evidence_show["id"] == evidence["id"], evidence_show

explain = load("explain-hermes.json")
assert explain["result_count"] >= 1, explain

print("archive smoke ok")
PY
