#!/usr/bin/env bash
# Regenerate the README quickstart recording (docs/assets/quickstart.cast + .svg)
# from a real run of the CURRENT build. Run this as part of a release cut,
# AFTER the version bump, so the recorded `brigade --version` matches the
# release; scripts/version_sync.py --check fails otherwise.
#
# Requires: svg-term on PATH (npm i -g svg-term-cli), python3, a brigade
# entry point (defaults to .venv/bin/brigade).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BRIGADE="${BRIGADE_BIN:-$ROOT/.venv/bin/brigade}"
CAST="$ROOT/docs/assets/quickstart.cast"
SVG="$ROOT/docs/assets/quickstart.svg"
WIDTH=84
HEIGHT=28

command -v svg-term >/dev/null || { echo "error: svg-term not on PATH" >&2; exit 2; }
[ -x "$BRIGADE" ] || { echo "error: brigade not found at $BRIGADE (set BRIGADE_BIN)" >&2; exit 2; }

demo="$(mktemp -d)"
trap 'rm -rf "$demo"' EXIT
git init -q -b main "$demo/my-repo"

version_out="$("$BRIGADE" --version)"
quickstart_out="$(cd "$demo/my-repo" && HOME="$demo/home" "$BRIGADE" operator quickstart --target . --harnesses codex 2>&1)"
doctor_out="$(cd "$demo/my-repo" && HOME="$demo/home" "$BRIGADE" operator doctor --target . --profile local-operator 2>&1)"

python3 - "$CAST" <<PYEOF
import json, sys

cast_path = sys.argv[1]
version_out = """$version_out"""
quickstart_out = """$quickstart_out"""
doctor_out = """$doctor_out"""

frames = []
clock = 0.6

def type_command(text):
    global clock
    frames.append([round(clock, 2), "o", "[1;32m\$[0m "])
    for ch in text:
        clock += 0.03
        frames.append([round(clock, 2), "o", ch])
    clock += 0.4
    frames.append([round(clock, 2), "o", "\r\n"])

def emit_output(text):
    global clock
    for line in text.splitlines():
        clock += 0.06
        frames.append([round(clock, 2), "o", line + "\r\n"])
    clock += 0.8

type_command("brigade --version")
emit_output(version_out)
type_command("brigade operator quickstart --target my-repo --harnesses codex")
emit_output(quickstart_out)
type_command("brigade operator doctor --target my-repo --profile local-operator")
emit_output(doctor_out)
clock += 2.4
frames.append([round(clock, 2), "o", ""])

header = {
    "version": 2,
    "width": $WIDTH,
    "height": $HEIGHT,
    "timestamp": 0,
    "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"},
}
with open(cast_path, "w") as fh:
    fh.write(json.dumps(header) + "\n")
    for frame in frames:
        fh.write(json.dumps(frame) + "\n")
print(f"wrote {cast_path} ({len(frames)} frames)")
PYEOF

svg-term --in "$CAST" --out "$SVG" --window --width "$WIDTH" --height "$HEIGHT"
echo "wrote $SVG"
python3 "$ROOT/scripts/version_sync.py" --check
