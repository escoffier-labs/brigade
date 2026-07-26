#!/usr/bin/env bash
# Grok/T3 Brigade work-loop hook (issue #536).
#
# Discovery rule: only a directory with .brigade/config.json is a project
# work root. ~/.brigade (user-level aboyeur roster) must never match.
set -u

payload="$(cat)"
event="$(jq -r '.hookEventName // empty' <<<"$payload" 2>/dev/null || true)"
session_id="$(jq -r '.sessionId // empty' <<<"$payload" 2>/dev/null || true)"
workspace="$(jq -r '.workspaceRoot // .cwd // empty' <<<"$payload" 2>/dev/null || true)"
tool_name="$(jq -r '.toolName // empty' <<<"$payload" 2>/dev/null || true)"
tool_command="$(jq -r '.toolInput.command // empty' <<<"$payload" 2>/dev/null || true)"

event="${event,,}"
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
  jq -cn --arg reason "$1" '{decision:"deny",reason:$reason}'
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
  while [[ -n "$path" && "$path" != "/" ]]; do
    if [[ -f "$path/.brigade/config.json" ]]; then
      printf '%s\n' "$path"
      return 0
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
  grep -Eiq '(^|[;&|[:space:]])(\.?/?scripts/verify|pytest|uv[[:space:]]+run[[:space:]]+pytest|python[0-9.]*[[:space:]]+-m[[:space:]]+pytest|ruff|mypy|pyright|eslint|tsc|vitest|jest|npm[[:space:]]+(run[[:space:]]+)?(test|lint|build|check|typecheck)|pnpm[[:space:]]+(run[[:space:]]+)?(test|lint|build|check|typecheck)|yarn[[:space:]]+(run[[:space:]]+)?(test|lint|build|check|typecheck)|bun[[:space:]]+(run[[:space:]]+)?(test|lint|build|check|typecheck)|go[[:space:]]+(test|vet|build)|cargo[[:space:]]+(test|check|clippy|build)|make[[:space:]]+(test|check|verify|lint|build)|just[[:space:]]+(test|check|verify|lint|build))([[:space:]]|$)' <<<"$tool_command"
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
      # Bound the brief like Claude's SessionStart timeout; never block edits on it.
      if timeout "$brief_timeout_seconds" "$brigade_bin" work brief --target "$target" >"$state_dir/brief.log" 2>&1; then
        touch "$state_dir/brief.done"
      else
        touch "$state_dir/brief.failed"
      fi
    ) >>"$log_file" 2>&1 &
    ;;

  pre_tool_use)
    if [[ "$tool_name" == "run_terminal_command" || "$tool_name" == "Bash" ]]; then
      if [[ "$tool_command" == *"brigade work brief"* || "$tool_command" == *"brigade daily status"* ]]; then
        allow
      fi
      if [[ "$tool_command" != *"brigade work verify run"* ]] && is_raw_verification; then
        deny "This repository is Brigade-wired. Run the check through: brigade work verify run --target . --command \"<test command>\" --capture brigade-work"
      fi
    fi
    # Do not deny edits while a background brief is running (#536).
    allow
    ;;

  post_tool_use)
    if [[ "$tool_name" == "run_terminal_command" || "$tool_name" == "Bash" ]]; then
      if [[ "$tool_command" == *"brigade work brief"* || "$tool_command" == *"brigade daily status"* ]]; then
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
