---
name: orca-governed-delegation
description: Use when a Brigade coordinator delegates work to local Orca workers through brigade orca. Follow the governed open, claim, worker-start, accepted, observe, ack and finalize lifecycle, and use cancel, hold-resolve and status for recovery and capacity. Checked against Orca 1.4.269.
allowed-tools: Read, Grep, Bash(brigade orca:*), Bash(brigade roster ready:*)
compatibility: A Linux workspace with Brigade Orca support and a compatible Orca CLI. The command is orca-ide because /usr/bin/orca is the screen reader.
---

# orca-governed-delegation

The coordinator owns every Orca CLI call. Brigade owns the durable enrollment,
launch claim, receipt correlation, observation, acknowledgement and closeout.
This skill was checked against Orca 1.4.269. On Linux, invoke Orca as
`orca-ide`: `/usr/bin/orca` belongs to the screen reader.

Placeholders: `<batch.json>` is a `brigade.run_batch.v1` batch;
`<run>` and `<task>` are Brigade identities from `open`; `<orca-run>` and
`<orca-task>` are the existing Orca Run and Task; JSON paths hold full native
receipts; `<result-id>` comes from a Brigade terminal result.

## Call order

Before the initial claim, run
`brigade roster ready --route orca --worker <seat> --target <workspace> --json`
on the coordinator with the PATH you will use to launch. Proceed only on
`ready`. After upgrading Orca, set the seat's `orca_version` to the exact
installed version, rerun readiness, and use a fresh enrollment for new work.
Keep the original enrollment and retry keys for retained work.

1. Enroll the batch: `brigade orca open <batch.json> --target <workspace>`. It prints a
   Brigade run and task identity and launches nothing. If it refuses with
   `orca-unsupported`, route that work through `brigade run batch`. Other
   refusals are not permission to try another path.
2. Claim exactly one task:
   `brigade orca claim <run> <task> --orca-run <orca-run> --orca-task <orca-task> --target <workspace>`.
   The output contains the exact `orca-ide orchestration worker-start` argv.
3. Run that printed argv as the coordinator. Preserve it exactly, including
   `--retry-request`, the existing Run and Task, and its other arguments.
4. Record the full worker-start JSON and its exit code:
   `brigade orca accepted <run> <task> --native-json <receipt.json> --exit-code <code> --target <workspace>`.
   Report every receipt to Brigade, including refusals. An unreported start is
   a worker Brigade cannot see. Definite refusals (`start-refused`,
   `circuit-broken`, or `start-failed`) return a `result_id`. Acknowledge it
   with `brigade orca ack <run> <task> --result-id <result-id> --target <workspace>`;
   until then the task holds capacity as `awaiting-ack`.
5. For a correlated worker, run the exact `observe_argv` printed by `accepted`
   to capture full `worker-show --json --full` evidence. Submit it first:
   `brigade orca observe <run> <task> --native-json <show.json> --target <workspace>`.
   The `observe` result prints the exact `observe_argv`, `inbox_argv` and
   `transcript_argv`. Run those exact Orca argv, save their JSON, then run
   `brigade orca observe <run> <task> --native-json <show.json> --worker-done-json <inbox.json> --transcript-json <transcript.json> --target <workspace>`.
   The transcript argv is for
   `orca-ide orchestration worker-read --environment local --dispatch <id> --source transcript --json`.
   Do not hand-build these Orca commands: the printed `inbox_argv` includes
   the required `--run` and `--limit` arguments. Read mail with `inbox --full`,
   never `check`, because `check` marks mail read. `worker-show` alone is not
   acceptance. The 1.4.269 "brief may not have been submitted" notice is
   neither acceptance nor acknowledgement. Without the matching worker_done
   report and complete transcript, observation remains
   `result-transfer-missing` and no terminal result is available to ack.
6. For each exact terminal result, acknowledge the Brigade result:
   `brigade orca ack <run> <task> --result-id <result-id> --target <workspace>`.
7. For a normally completed or failed worker, after Orca releases it, run a
   fresh full `worker-show` using the printed `observe_argv`, then repeat
   `observe` with that show, the matching worker_done report and transcript.
   For a cancelled worker, after a confirmed stop, use the fresh full show
   alone to verify release:
   `brigade orca observe <run> <task> --native-json <show.json> --target <workspace>`.
   A cancelled worker may not produce a worker_done report or transcript.
   Finalize only after the post-release observation proves release; otherwise
   `release-unverified` blocks closeout. Then close the settled run explicitly:
   `brigade orca finalize <run> --target <workspace>`.

## Recovery and capacity

- After a lost reply, reuse the same `--retry-request` id and exact printed
  worker-start argv. Never start a new Orca Run or Task to get around the
  circuit breaker. Do not invent a new request id or launch a replacement.
- `outcome_unknown` means the start outcome is uncertain. Use
  `brigade orca hold-resolve <run> <task> --native-json <receipt.json> --exit-code <code> --show-json <show.json> --target <workspace>`
  only with recovered start evidence. `hold-resolve` can also clear an
  `accept-conflict` hold when the recovered evidence matches the retained
  worker. `brigade orca hold-resolve <run> <task> --abandon --target <workspace>`
  writes a permanent `abandoned-unknown` hold. It frees no slot, `finalize`
  refuses it, and an operator must follow up. Do not use `hold-resolve` to
  settle a worker stop.
- For a full `failed` worker-start receipt with residual resources, keep the
  original receipt and its exit code. Capture a fresh `worker-show --json --full`
  for the same dispatch after release. Supply the receipt with `--native-json`
  and the snapshot with `--show-json` to `accepted` or `hold-resolve`. A
  snapshot that proves release and exited liveness settles `start-failed`.
  Acknowledge the returned result ID to free the slot before `finalize`.
  If either proof is missing, `start-unknown` still holds the slot.
- Request cancellation with `brigade orca cancel <run> <task> --target <workspace>`;
  run the printed `orca-ide orchestration worker-stop` argv. If it reports
  `stop_unknown`, rerun that same printed argv with the same `--retry-request`,
  then report again through `cancel`. A stop request is not proof of
  cancellation. The full report form is
  `brigade orca cancel <run> <task> --native-json <stop.json> --exit-code <code> --show-json <show.json> --target <workspace>`.
  `--native-json` and `--exit-code` must be supplied together.
- Inspect state and workspace capacity with
  `brigade orca status <run> --json --target <workspace>`. Status is read-only.
- Only Brigade `ack` acknowledges a terminal result. Orca worker state, mail,
  delivery notices and a start receipt do not substitute for it.

## Commands

The supported Brigade verbs are `open`, `claim`, `accepted`, `observe`, `ack`,
`finalize`, `cancel`, `hold-resolve` and `status`. Follow each printed native
argv exactly and retain its receipts for the matching Brigade verb.
