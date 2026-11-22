---
name: orca-governed-delegation
description: Use when a Brigade coordinator delegates work to local Orca workers through brigade orca. Follow the governed open, claim, worker-start, accepted, observe, ack and finalize lifecycle, and use cancel, hold-resolve and status for recovery and capacity. Checked against Orca 1.4.269.
allowed-tools: Read, Grep, Bash(brigade orca:*)
compatibility: A Linux workspace with Brigade Orca support and the Orca 1.4.269 CLI. The command is orca-ide because /usr/bin/orca is the screen reader.
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

1. Enroll the batch: `brigade orca open <batch.json> --target .`. It prints a
   Brigade run and task identity and launches nothing. If it refuses with
   `orca-unsupported`, route that work through `brigade run batch`. Other
   refusals are not permission to try another path.
2. Claim exactly one task:
   `brigade orca claim <run> <task> --orca-run <orca-run> --orca-task <orca-task> --target .`.
   The output contains the exact `orca-ide orchestration worker-start` argv.
3. Run that printed argv as the coordinator. Preserve it exactly, including
   `--retry-request`, the existing Run and Task, and its other arguments.
4. Record the full worker-start JSON and its exit code:
   `brigade orca accepted <run> <task> --native-json <receipt.json> --exit-code <code> --target .`.
   Report every receipt to Brigade, including refusals. An unreported start is
   a worker Brigade cannot see.
5. Observe using full `worker-show --json --full` evidence and completion mail
   from `orca-ide orchestration inbox --full --json`:
   `brigade orca observe <run> <task> --native-json <show.json> --worker-done-json <inbox.json> --target .`.
   Read mail with `inbox --full`, never `check`, because `check` marks mail
   read. `worker-show` alone is not acceptance. The 1.4.269 "brief may not have
   been submitted" notice is neither acceptance nor acknowledgement.
6. For each exact terminal result, acknowledge the Brigade result:
   `brigade orca ack <run> <task> --result-id <result-id> --target .`.
7. Close the settled run explicitly:
   `brigade orca finalize <run> --target .`.

## Recovery and capacity

- After a lost reply, reuse the same `--retry-request` id and exact printed
  worker-start argv. Never start a new Orca Run or Task to get around the
  circuit breaker. Do not invent a new request id or launch a replacement.
- `outcome_unknown` means the start outcome is uncertain. Use
  `brigade orca hold-resolve <run> <task> --native-json <receipt.json> --exit-code <code> --show-json <show.json> --target .`
  only with recovered evidence, or `--abandon` when the launch cannot be
  correlated. `stop_unknown` and `outcome_unknown` keep the task open and hold
  capacity. Do not acknowledge or finalize them as settled.
- Request cancellation with
  `brigade orca cancel <run> <task> --target .`; run the printed
  `orca-ide orchestration worker-stop` argv, then report its receipt and fresh
  full `worker-show` evidence with the same verb. A stop request is not proof
  of cancellation.
- Inspect state and workspace capacity with
  `brigade orca status <run> --json --target .`. Status is read-only.
- Only Brigade `ack` acknowledges a terminal result. Orca worker state, mail,
  delivery notices and a start receipt do not substitute for it.

## Commands

The supported Brigade verbs are `open`, `claim`, `accepted`, `observe`, `ack`,
`finalize`, `cancel`, `hold-resolve` and `status`. Follow each printed native
argv exactly and retain its receipts for the matching Brigade verb.
