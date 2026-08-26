#!/usr/bin/env bash
# Grok/T3 Brigade work-loop hook (issue #536).
#
# Discovery rule: only a directory with .brigade/config.json is a project
# work root. ~/.brigade (user-level aboyeur roster) must never match.
set -u

payload="$(cat)"
event=""
session_id=""
workspace=""
tool_name=""
tool_command=""
hook_fields="$(
  printf '%s' "$payload" | python3 -c '
import json
import shlex
import sys

try:
    payload = json.load(sys.stdin)
except (TypeError, ValueError):
    payload = {}

tool_input = payload.get("toolInput")
if not isinstance(tool_input, dict):
    tool_input = {}

def text(value):
    return value if isinstance(value, str) else ""

fields = {
    "event": text(payload.get("hookEventName")),
    "session_id": text(payload.get("sessionId")),
    "workspace": text(payload.get("workspaceRoot")) or text(payload.get("cwd")),
    "tool_name": text(payload.get("toolName")),
    "tool_command": text(tool_input.get("command")),
}
for name, value in fields.items():
    print(f"{name}={shlex.quote(value)}")
'
)" || hook_fields=""
if [[ -n "$hook_fields" ]]; then
  # Values are shell-quoted by the Python parser above, not by hook input.
  eval "$hook_fields"
fi

event="$(printf '%s' "$event" | tr '[:upper:]' '[:lower:]')"
session_id="${session_id:-unknown-session}"
safe_session="$(printf '%s' "$session_id" | tr -c 'A-Za-z0-9._-' '_')"
brigade_bin="${BRIGADE_BIN:-$(command -v brigade 2>/dev/null || true)}"
state_root="${GROK_BRIGADE_STATE_DIR:-$HOME/.grok/state/brigade-work}"
state_dir="$state_root/$safe_session"
brief_timeout_seconds="${BRIGADE_GROK_BRIEF_TIMEOUT_SECONDS:-30}"

allow() {
  printf '{"decision":"allow"}\n'
  exit 0
}

deny() {
  python3 -c 'import json, sys; print(json.dumps({"decision": "deny", "reason": sys.argv[1]}))' "$1"
  exit 0
}

find_brigade_target() {
  local path="$1"
  [[ -n "$path" ]] || return 1
  # Prefer the shared resolver so shell hooks cannot reimplement discovery wrong.
  if [[ -n "$brigade_bin" ]] && "$brigade_bin" work resolve-target --help >/dev/null 2>&1; then
    if target="$("$brigade_bin" work resolve-target --cwd "$path" --harness grok 2>/dev/null)"; then
      [[ -n "$target" ]] || return 1
      printf '%s\n' "$target"
      return 0
    fi
    return 1
  fi
  # Fallback for older Brigade installs: require config.json, never match roster-only ~/.brigade.
  path="$(cd "$path" 2>/dev/null && pwd -P)" || return 1
  local home="${HOME:-}"
  if [[ -n "$home" ]]; then
    home="$(cd "$home" 2>/dev/null && pwd -P)" || home=""
  fi
  while [[ -n "$path" && "$path" != "/" ]]; do
    if [[ -f "$path/.brigade/config.json" ]]; then
      if python3 - "$path/.brigade/config.json" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as config_file:
        config = json.load(config_file)
except (OSError, TypeError, ValueError):
    raise SystemExit(1)

harnesses = config.get("harnesses") if isinstance(config, dict) else None
selected = {
    harness.strip().lower()
    for harness in harnesses
    if isinstance(harness, str)
} if isinstance(harnesses, list) else set()
raise SystemExit(0 if "grok" in selected else 1)
PY
      then
        printf '%s\n' "$path"
        return 0
      fi
      # Match resolve-target: the first project config is authoritative.
      return 1
    fi
    if [[ -n "$home" && "$path" == "$home" ]]; then
      break
    fi
    path="$(dirname "$path")"
  done
  return 1
}

target="$(find_brigade_target "$workspace" 2>/dev/null || true)"
if [[ -z "$target" || -z "$brigade_bin" ]]; then
  if [[ "$event" == "pre_tool_use" ]]; then
    allow
  fi
  exit 0
fi

mkdir -p "$state_dir" "$state_root/logs"
log_file="$state_root/logs/$safe_session.log"
printf '%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$event" "$tool_name" >>"$log_file"

is_edit_tool() {
  case "$tool_name" in
    search_replace|write_file|apply_patch|Edit|Write|MultiEdit) return 0 ;;
    *) return 1 ;;
  esac
}

is_raw_verification() {
  [[ -n "$tool_command" ]] || return 1
  grep -Eiq '(^|[;&|[:space:]])(\.?/?scripts/(verify-focused|verify)|pytest|uv[[:space:]]+run[[:space:]]+pytest|python[0-9.]*[[:space:]]+-m[[:space:]]+pytest|ruff|mypy|pyright|eslint|tsc|vitest|jest|npm[[:space:]]+(run[[:space:]]+)?(test|lint|build|check|typecheck)|pnpm[[:space:]]+(run[[:space:]]+)?(test|lint|build|check|typecheck)|yarn[[:space:]]+(run[[:space:]]+)?(test|lint|build|check|typecheck)|bun[[:space:]]+(run[[:space:]]+)?(test|lint|build|check|typecheck)|go[[:space:]]+(test|vet|build)|cargo[[:space:]]+(test|check|clippy|build)|make[[:space:]]+(test|check|verify|lint|build)|just[[:space:]]+(test|check|verify|lint|build))([[:space:]]|$)' <<<"$tool_command"
}

is_direct_brigade_verification() {
  [[ "$tool_command" =~ ^[[:space:]]*brigade[[:space:]]+work[[:space:]]+verify[[:space:]]+run([[:space:]]|$) ]] || return 1
  is_simple_shell_command
}

is_direct_brigade_status() {
  [[ "$tool_command" =~ ^[[:space:]]*brigade[[:space:]]+(work[[:space:]]+brief|daily[[:space:]]+status)([[:space:]]|$) ]] || return 1
  is_simple_shell_command
}

is_simple_shell_command() {
  case "$tool_command" in
    *';'*|*'&&'*|*'||'*|*'|'*|*'`'*|*'$('*|*'>'*|*'<') return 1 ;;
  esac
  return 0
}

run_brief_bounded() {
  python3 - "$brief_timeout_seconds" "$brigade_bin" "$target" "$state_dir/brief.log" <<'PY'
import os
import signal
import subprocess
import sys

try:
    timeout = float(sys.argv[1])
except ValueError:
    timeout = 30.0
if timeout <= 0:
    timeout = 30.0

with open(sys.argv[4], "w", encoding="utf-8") as brief_log:
    process = subprocess.Popen(
        [sys.argv[2], "work", "brief", "--target", sys.argv[3]],
        stdout=brief_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        raise SystemExit(process.wait(timeout=timeout))
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise SystemExit(124)
PY
}

has_new_receipt() {
  [[ -f "$state_dir/substantive" ]] || return 1
  [[ -d "$target/.brigade/work/verify-runs" ]] || return 1
  find "$target/.brigade/work/verify-runs" -type f -name receipt.json -newer "$state_dir/substantive" -print -quit 2>/dev/null | grep -q .
}

close_out_work() {
  [[ -f "$state_dir/substantive" ]] || return 0
  if has_new_receipt; then
    "$brigade_bin" receipts export miseledger --target "$target" --new-only --import >>"$log_file" 2>&1 || true
    rm -f "$state_dir/substantive" "$state_dir/friction.reported"
  elif [[ ! -f "$state_dir/friction.reported" ]]; then
    "$brigade_bin" friction add --target "$target" --type workflow_correction --severity high --workflow grok/brigade-work --evidence "grok-session:$session_id" "Grok session $session_id ended after edits without a new Brigade verify receipt" >>"$log_file" 2>&1 || true
    touch "$state_dir/friction.reported"
  fi
}

case "$event" in
  session_start)
    printf '%s\n' "$target" >"$state_dir/target"
    touch "$state_dir/started"
    rm -f "$state_dir/brief.done" "$state_dir/substantive" "$state_dir/ended" "$state_dir/brief.failed"
    (
      # Python's process-group timeout works on BSD and GNU userlands.
      if run_brief_bounded; then
        touch "$state_dir/brief.done"
      else
        touch "$state_dir/brief.failed"
      fi
    ) >>"$log_file" 2>&1 &
    ;;

  pre_tool_use)
    if [[ "$tool_name" == "run_terminal_command" || "$tool_name" == "Bash" ]]; then
      if is_direct_brigade_status; then
        allow
      fi
      if ! is_direct_brigade_verification && is_raw_verification; then
        deny "This repository is Brigade-wired. Run the check through: brigade work verify run --target . --command \"<test command>\" --capture brigade-work"
      fi
    fi
    # Do not deny edits while a background brief is running (#536).
    allow
    ;;

  post_tool_use)
    if [[ "$tool_name" == "run_terminal_command" || "$tool_name" == "Bash" ]]; then
      if is_direct_brigade_status; then
        touch "$state_dir/brief.done"
      fi
    fi
    if is_edit_tool; then
      touch "$state_dir/substantive"
      rm -f "$state_dir/friction.reported"
    fi
    ;;

  stop)
    close_out_work
    ;;

  session_end)
    [[ -f "$state_dir/ended" ]] && exit 0
    touch "$state_dir/ended"
    close_out_work
    ;;
esac

exit 0
