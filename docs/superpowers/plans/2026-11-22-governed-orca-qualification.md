# Brigade-Governed Orca Qualification Plan

> **For the qualification coordinator:** Run the batches on the machine with
> Orca installed. This implementation slice supplies the plan and offline
> collector. Live evidence belongs in the follow-up to #2884.

**Goal:** Compare verified completion and operational overhead for governed
Orca and explicit Brigade batch execution under matched conditions.

**Architecture:** In arm A, the local Orca coordinator executes the exact
native commands issued by Brigade. In arm B, Brigade executes an explicit
batch on the same worker seat. Both arms record durable task observations in
the Brigade task journal.

**Tech Stack:** Orca 1.4.269, the pinned Brigade checkout, local Git worktrees
and the standard-library collector.

**Spec:** [Governed Orca delegation](../../orca-governed-delegation.md) and
[Brigade-owned governance and recovery](../../phase-2046-governed-native-child-contract.md).

## Global constraints

- Use a dedicated local workspace with no competing governed workers. Keep
  all raw receipts and the roster private, outside the source checkout.
- Pin the Brigade executable commit and workload source revision separately.
  Match the same seat, model, effort, task inputs, worktree isolation and
  concurrency of three throughout the experiment. Arm A asks workers to leave
  the checkout unchanged in the prompt. Its worker-start argv has no read-only
  enforcement. Arm B requires hard read-only enforcement through Brigade's
  batch policy. Verify both arms' worktrees stayed clean after task execution
  and record the enforcement levels with the results.
- Use Codex GPT 6.2 Sol at medium effort for the first series. Codex Luna 6
  or opencode DeepSeek V4.1 Flash may be evaluated in a separate matched
  series only if both routes support the same selection and isolation.
  Orca 1.4.269 does not support new worktrees with opencode or an explicit
  opencode effort pin. Use no Claude or Fable seats.
- Run the pairs in this order: **A/B, B/A, A/B, B/A, A/B**. Start each batch
  only after the prior batch has settled and capacity is clear. Keep failed
  or incomplete attempts in the report. A repeat is a separately recorded
  attempt after recovery, never a replacement for an uncertain launch.
- Continue only after readiness passes for each route. No speedup threshold
  applies.

## Task 1: Freeze the workload and configuration

In the dedicated source checkout, commit three UTF-8 files under
`qualification-inputs/` using the text below, with one LF-terminated line
per field. Pin that commit as the source revision. Use the same checkout and
revision in all batches. Confirm each worker's checkout starts at that revision. Check its worktree
for changes after execution and before release or disposal.

| File | First line | Second line | Exact expected result |
| --- | --- | --- | --- |
| alpha.txt | `Item: apple` | `Count: 2` | `alpha\|apple\|2` |
| beta.txt | `Item: pear` | `Count: 5` | `beta\|pear\|5` |
| gamma.txt | `Item: plum` | `Count: 3` | `gamma\|plum\|3` |

Use the following batch template. Replace only `request_id` with a unique
identifier per attempt (for example, `pair-01-a`). Copy each exact `text`
to its matching existing task in the local coordinator's Orca Run. Stage a
fresh Run and its three Tasks in Orca for each planned arm A attempt before
timing. Do not start the workers during staging.

```json
{
  "schema": "brigade.run_batch.v1",
  "request_id": "pair-01-a",
  "tasks": [
    {"task_id": "alpha", "seat": "worker", "isolation": "worktree", "read_only": true, "text": "Read only qualification-inputs/alpha.txt. Do not edit files. Return one line: alpha|<Item value>|<Count value>."},
    {"task_id": "beta", "seat": "worker", "isolation": "worktree", "read_only": true, "text": "Read only qualification-inputs/beta.txt. Do not edit files. Return one line: beta|<Item value>|<Count value>."},
    {"task_id": "gamma", "seat": "worker", "isolation": "worktree", "read_only": true, "text": "Read only qualification-inputs/gamma.txt. Do not edit files. Return one line: gamma|<Item value>|<Count value>."}
  ],
  "synthesis": null
}
```

Use the workspace's `.brigade/roster.toml` for both arms, including status
queries. Configure `limits.max_workers = 3` and `limits.sandbox = "read-only"`.
Pin the worker's Codex model and
`reasoning = "medium"` in `[agents.worker]`. Set the following native binding.
Use the exact model identifier accepted by both providers and confirm the
effective model and effort from local evidence before accepting a match.

```toml
[orca_targets.worker]
agent = "codex"
orca_version = "1.4.269"
worktree = "new-child"
model = "gpt-6.2-sol"
effort = "medium"
max_live = 3
```

In the commands below, set `ws` to the source checkout, `batch` to the
attempt's input JSON and `out` to a private per-attempt receipt directory.
Use the same pinned `brigade` executable for all commands. From the
qualification checkout, use its Python environment to run the collector.
Verify version and readiness outside the timed interval.

```bash
orca-ide --version
brigade roster ready --route orca --worker worker --concurrency 3 --isolation worktree --target "$ws" --json
brigade roster ready --route brigade --worker worker --concurrency 3 --isolation worktree --target "$ws" --json
```

## Task 2: Execute each matched pair

### Arm A: Governed Orca

Start the batch clock immediately before `open`. Set `run` from its
response and bind each Brigade task to its already staged native task.

```bash
brigade orca open "$batch" --target "$ws" > "$out/open.json"
brigade orca claim "$run" "$task" --orca-run "$orca_run" --orca-task "$orca_task" --target "$ws" > "$out/$task-claim.json"
```

For each task, use the saved claim's `retry_request` as the retry key and
execute its exact `argv` array. This runs `orca-ide orchestration worker-start`
with Brigade's pinned target and `--retry-request` value. Save the full JSON
and actual exit code. Dispatch the three tasks without awaiting completion
between starts.

```bash
key=$(python -c 'import json, sys; print(json.load(open(sys.argv[1]))["retry_request"])' "$out/$task-claim.json")
if python -c 'import json, subprocess, sys; sys.exit(subprocess.run(json.load(open(sys.argv[1]))["argv"], check=False).returncode)' "$out/$task-claim.json" > "$out/$task-start.json"; then
  start_rc=0
else
  start_rc=$?
fi
brigade orca accepted "$run" "$task" --native-json "$out/$task-start.json" --exit-code "$start_rc" --target "$ws" > "$out/$task-accepted.json"
```

If `accepted` returns a definite `circuit-broken`, `start-refused` or
`start-failed` outcome, take `result_id` from that response and acknowledge
it immediately. This releases the slot held for acknowledgement. Count it
as an error and skip worker observation for that refusal.

```bash
brigade orca ack "$run" "$task" --result-id "$result_id" --target "$ws"
```

A lost reply reuses the same saved argv and the same `--retry-request`
value in `$key`. Follow the governed recovery procedure and keep the
original claim and receipts.

On a coordinator wake, run the exact returned `observe_argv` into the
task's show file. Observe it first, then use the returned `observe_argv`,
`inbox_argv` and `transcript_argv` to populate the three receipt files below.
Use the same receipt names with fresh show evidence after resource release.
Read and verify the exact worker result against the table before declaring
verified completion.

```bash
brigade orca observe "$run" "$task" --native-json "$out/$task-show.json" --target "$ws"
brigade orca observe "$run" "$task" --native-json "$out/$task-show.json" --worker-done-json "$out/$task-inbox.json" --transcript-json "$out/$task-transcript.json" --target "$ws"
brigade orca ack "$run" "$task" --result-id "$result_id" --target "$ws"
# After release, capture a fresh show using observe_argv, then submit it.
brigade orca observe "$run" "$task" --native-json "$out/$task-release.json" --worker-done-json "$out/$task-inbox.json" --transcript-json "$out/$task-transcript.json" --target "$ws"
brigade orca finalize "$run" --target "$ws"
brigade orca status "$run" --json --target "$ws" > "$out/orca-status.json"
```

For stop requests, release uncertainty or other exceptions, follow
the recovery commands in the
[governed delegation guide](../../orca-governed-delegation.md).
Save status even when finalization refuses. Never bypass the circuit breaker
with a fresh Run or Task, launch through the baseline as recovery, or count
`outcome_unknown`, `stop_unknown` or unverifiable liveness as terminal.

### Arm B: Explicit Brigade batch

Start the clock immediately before `run batch`. Save its JSON and exit code
even on failure. Obtain `run` from the response. Verify each task's retained
worker receipt at its `result_ref` against the expected line.

```bash
brigade run batch "$batch" --cwd "$ws" --json > "$out/batch-result.json"
brigade orca status "$last_orca_run" --json --target "$ws" > "$out/orca-status.json"
```

Use the most recent governed run for `last_orca_run`, even for B-first
pairs. Inspect the fresh **workspace-wide** `capacity.live` value after
each arm, as well as the listed task holds. Require zero and no held tasks.
An unavailable capacity reading fails the slot check.

## Task 3: Collect and review the records

Use the Brigade run directory under the workspace's `.brigade/runs/` for
the current attempt. Keep raw native replies, the Brigade run files and the
measurement sheet local. Use monotonic elapsed time for coordinator-observed
intervals. For task phases, record the elapsed duration from the earliest
observed phase start to the latest observed phase end across all three tasks
only when both endpoints are available for each applicable task. Phase
windows can overlap. Never sum them into batch wall time.

| Metric | Measurement |
| --- | --- |
| Queue | Submission to admission or dispatch approval |
| Startup | Approved launch to worker ready |
| Planning | Worker ready to recorded plan completion, when exposed |
| Execution | Start of task work to worker completion |
| Synthesis | Disabled in both arms, record it as null |
| Delivery | Worker completion to coordinator-visible retained result |
| Verified completion | Batch submission through output verification, terminal closeout and zero-capacity check |
| Usage | Sum observed input, output and cached-input token counters across workers and attempts, with null for incomplete counters |
| Parent interventions | Count exceptional actions like a retry, recovery, cancellation or manual unblock. Routine claim, observation, acknowledgement and verification are excluded |
| Other errors | Count command or verification errors that are not already captured in a task outcome or circuit-breaker count |

Build `measurements.json` from observed local timings and usage, using the
following shape. Leave unobserved values null. Provider start replies and
transcripts may expose different evidence than baseline worker receipts.
Use only observed phase boundaries and usage, with no inference from adjacent
timestamps or log length. Set verification true only if all three outputs
match and the worktrees stayed clean. Set the circuit breaker field to false
only after reviewing coordinator and receipt history for bypasses.

```json
{
  "orca_version": "1.4.269",
  "brigade_commit": "<full commit SHA of the Brigade executable>",
  "source_revision": "<full workload checkout SHA>",
  "model": "gpt-6.2-sol",
  "effort": "medium",
  "concurrency": 3,
  "isolation": "worktree",
  "verification_passed": false,
  "verified_completion_seconds": null,
  "circuit_breaker_routed_around": null,
  "parent_interventions": 0,
  "other_errors": 0,
  "timings_seconds": {"queue": null, "startup": null, "planning": null, "execution": null, "synthesis": null, "delivery": null},
  "usage": {"input_tokens": null, "output_tokens": null, "cached_input_tokens": null}
}
```

```bash
python -m brigade.orca_qualification "$arm" "$run_dir" "$out/orca-status.json" "$out/measurements.json" > "$out/record.json"
```

Set `arm` to `orca` or `run-batch`. The collector uses journal-backed
task outcomes and the saved Orca status, plus the coordinator measurements.
It compares the sheet's model, effort, revision, concurrency and isolation
with local run evidence. If any field disagrees or lacks corroboration,
`configuration_mismatch` is true, `mismatched_fields` names those fields,
and verified completion time is null. Concurrency comes from the saved
workspace worker limit. Check the coordinator's per-seat limit and dispatch
order in the local evidence as well. Arm B also needs a successful terminal
run status.
It emits a closed record of aggregate counts, timings and pinned settings.
Only the projected record may leave the machine. It includes no prompt
bodies, credentials, paths or run/task identifiers. An error exit means
collection failed. Keep the local evidence and report the missing batch.

After five complete pairs, report each arm's outcomes and per-phase
observations in execution order. Check zero held capacity, terminal proof
and no bypass per batch. Show coverage and missing observations before
comparing timings. Update #2884 and #2800 only with supported findings.

## Planning verification

Run the offline collector fixtures and existing journal qualification tests
through Brigade verification. Use the PR's current-head CI for the broader
suite. Fixture passage supplies no live qualification evidence.
