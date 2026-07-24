#!/usr/bin/env bash
set -uo pipefail

if [[ $# -ne 3 ]]; then
  printf 'usage: %s CASE_ID SAMPLE_ID RETRY_ID\n' "$0" >&2
  exit 2
fi

case_id="$1"
sample_id="$2"
retry_id="$3"
experiment_root="$(cd "$(dirname "$0")" && pwd -P)"
repo_root="$(cd "$experiment_root/../.." && pwd -P)"
brigade_cli="${BRIGADE_EXPERIMENT_CLI:-$repo_root/.venv/bin/brigade}"
sandbox="${BRIGADE_EXPERIMENT_SANDBOX:-}"
query_file="$experiment_root/queries/case-$case_id.txt"
label="route-b-sample-$sample_id-retry-$retry_id"
output_dir="$experiment_root/raw/case-$case_id/route-b/sample-$sample_id-retry-$retry_id"
command_logs="$experiment_root/command-logs/case-$case_id"

if [[ -z "$sandbox" || ! -d "$sandbox/.git" && ! -f "$sandbox/.git" ]]; then
  printf 'BRIGADE_EXPERIMENT_SANDBOX must name a git worktree\n' >&2
  exit 2
fi
if [[ ! -f "$query_file" || ! -f "$sandbox/EVIDENCE.md" ]]; then
  printf 'missing query or sandbox evidence for case %s\n' "$case_id" >&2
  exit 2
fi
if [[ -e "$output_dir" || -e "$command_logs/$label.exit-code" ]]; then
  printf 'refusing to overwrite retry artifacts: %s\n' "$label" >&2
  exit 2
fi

task="$(<"$query_file")"
start_ns="$(date +%s%N)"
"$brigade_cli" run "$task" \
  --roster "$experiment_root/config/route-b.toml" \
  --worker sample \
  --output-dir "$output_dir" \
  --read-only \
  --sandbox read-only \
  --codex-transport exec \
  --cwd "$sandbox" \
  --no-evidence \
  --no-route \
  --allow-dirty \
  >"$command_logs/$label.stdout.log" \
  2>"$command_logs/$label.stderr.log"
status=$?
end_ns="$(date +%s%N)"
elapsed="$(awk -v start="$start_ns" -v end="$end_ns" 'BEGIN { printf "%.3f", (end-start)/1000000000 }')"
printf '%s\n' "$start_ns" >"$command_logs/$label.start-ns"
printf '%s\n' "$end_ns" >"$command_logs/$label.end-ns"
printf '%s\n' "$elapsed" >"$command_logs/$label.wall-seconds"
printf '%s\n' "$status" >"$command_logs/$label.exit-code"
exit "$status"
