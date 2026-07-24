#!/usr/bin/env bash
set -uo pipefail

if [[ $# -ne 1 ]]; then
  printf 'usage: %s CASE_ID\n' "$0" >&2
  exit 2
fi

case_id="$1"
experiment_root="$(cd "$(dirname "$0")" && pwd -P)"
repo_root="$(cd "$experiment_root/../.." && pwd -P)"
brigade_cli="${BRIGADE_EXPERIMENT_CLI:-$repo_root/.venv/bin/brigade}"
sandbox="${BRIGADE_EXPERIMENT_SANDBOX:-}"
prompt_file="$experiment_root/prompts/case-$case_id.md"
query_file="$experiment_root/queries/case-$case_id.txt"
case_output="$experiment_root/raw/case-$case_id"
command_logs="$experiment_root/command-logs/case-$case_id"

if [[ -z "$sandbox" || ! -d "$sandbox/.git" && ! -f "$sandbox/.git" ]]; then
  printf 'BRIGADE_EXPERIMENT_SANDBOX must name a git worktree\n' >&2
  exit 2
fi
if [[ ! -f "$prompt_file" ]]; then
  printf 'missing prompt: %s\n' "$prompt_file" >&2
  exit 2
fi
if [[ ! -f "$query_file" ]]; then
  printf 'missing query: %s\n' "$query_file" >&2
  exit 2
fi
if [[ ! -f "$sandbox/EVIDENCE.md" ]]; then
  printf 'missing sandbox evidence: %s\n' "$sandbox/EVIDENCE.md" >&2
  exit 2
fi
if [[ -e "$case_output" || -e "$command_logs" ]]; then
  printf 'refusing to overwrite existing case artifacts: %s\n' "$case_id" >&2
  exit 2
fi

mkdir -p "$case_output" "$command_logs"
task="$(<"$query_file")"
common=(
  --read-only
  --sandbox read-only
  --codex-transport exec
  --cwd "$sandbox"
  --no-evidence
  --no-route
  --allow-dirty
)

run_timed() {
  local label="$1"
  shift
  local start_ns end_ns elapsed
  start_ns="$(date +%s%N)"
  "$@" >"$command_logs/$label.stdout.log" 2>"$command_logs/$label.stderr.log"
  local status=$?
  end_ns="$(date +%s%N)"
  elapsed="$(awk -v start="$start_ns" -v end="$end_ns" 'BEGIN { printf "%.3f", (end-start)/1000000000 }')"
  printf '%s\n' "$start_ns" >"$command_logs/$label.start-ns"
  printf '%s\n' "$end_ns" >"$command_logs/$label.end-ns"
  printf '%s\n' "$elapsed" >"$command_logs/$label.wall-seconds"
  printf '%s\n' "$status" >"$command_logs/$label.exit-code"
  return "$status"
}

run_timed route-a \
  "$brigade_cli" run "$task" \
  --roster "$experiment_root/config/route-a.toml" \
  --output-dir "$case_output/route-a" \
  "${common[@]}"
route_a_status=$?

route_b_start_ns="$(date +%s%N)"
route_b_status=0
for sample in 1 2 3; do
  if ! run_timed "route-b-sample-$sample" \
    "$brigade_cli" run "$task" \
    --roster "$experiment_root/config/route-b.toml" \
    --worker sample \
    --output-dir "$case_output/route-b/sample-$sample" \
    "${common[@]}"; then
    route_b_status=1
  fi
done
route_b_end_ns="$(date +%s%N)"
route_b_elapsed="$(awk -v start="$route_b_start_ns" -v end="$route_b_end_ns" 'BEGIN { printf "%.3f", (end-start)/1000000000 }')"
printf '%s\n' "$route_b_start_ns" >"$command_logs/route-b.start-ns"
printf '%s\n' "$route_b_end_ns" >"$command_logs/route-b.end-ns"
printf '%s\n' "$route_b_elapsed" >"$command_logs/route-b.wall-seconds"
printf '%s\n' "$route_b_status" >"$command_logs/route-b.exit-code"

run_timed route-c \
  "$brigade_cli" run "$task" \
  --roster "$experiment_root/config/route-c.toml" \
  --output-dir "$case_output/route-c" \
  --deliberate \
  "${common[@]}"
route_c_status=$?

if [[ "$route_a_status" -ne 0 || "$route_b_status" -ne 0 || "$route_c_status" -ne 0 ]]; then
  exit 1
fi
