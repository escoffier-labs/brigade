---
name: t3-governed-delegation
description: Use when a T3 Code parent session delegates child tasks under Brigade governance - the exact t3 open, claim, accepted, observe, cancel, ack and finalize order, how task_status delivery differs from Brigade acknowledgement, and why a lost delegate reply must be held instead of resubmitted. Never resubmit delegate_task after a lost reply and never treat task_status as Brigade acknowledgement.
allowed-tools: Read, Grep, Bash(brigade t3:*), Bash(brigade roster:*)
compatibility: A T3 Code parent session with native orchestrator_capabilities, delegate_task, task_status and task_cancel tools, against a workspace enrolled by brigade t3 open.
---

# t3-governed-delegation

Run a Brigade-governed native T3 child task without letting Brigade call T3. The parent session owns every native call. Brigade owns the durable claim, correlation, observation, cancellation request, acknowledgement and closeout.

Placeholders: `<run>` (run id from `t3 open`), `<task>` (task id), `<parent-thread>` (the delegating T3 thread), `<result.json>` (a saved native projection).

## Call order

1. `brigade t3 open batch.json --target .` enrolls the batch and prints `run_id`. It launches nothing.
2. Before the first claim, call your own `orchestrator_capabilities` tool. Write its unmodified result into a `brigade.t3_capabilities_report.v1` envelope, then run
   `brigade roster ready --route t3 --isolation worktree --worker <seat> --t3-capabilities <file|->`.
   A ready result is parent-reported and non-reserving. Without the envelope, route support stays unknown.
3. For each task, in this order:
   - `brigade t3 claim <run> <task> --parent-thread <parent-thread>`
   - Submit the exact printed `delegate_task` arguments with your own native tool.
   - `brigade t3 accepted <run> <task> --parent-thread <parent-thread> --native-json <result.json>`
4. On a completion wake, call the native `task_status` tool, save the projection, then run
   `brigade t3 observe <run> <task> --parent-thread <parent-thread> --native-json <result.json>`.
5. When cancellation is needed, run `brigade t3 cancel <run> <task> --parent-thread <parent-thread>`, submit the printed `task_cancel` arguments, then call `task_status` and `brigade t3 observe` with the new projection.
6. For each terminal result, run `brigade t3 ack <run> <task> --result-id <id>`.
7. Run `brigade t3 finalize <run>`. It is never automatic.

Inspect progress with `brigade t3 status <run> --json`. It prints fixed tokens only: state, held reason, correlated, terminal, acked, cancel-requested, attempt and a seat digest.

## Readiness is parent-reported

`brigade roster ready --route t3 --t3-capabilities <file|->` checks the parent-reported capabilities envelope against each selected target and adds Brigade isolation, admission and capacity evidence. The envelope is `brigade.t3_capabilities_report.v1` with `schema`, `origin_environment`, `origin_project`, `reported_at` in ISO UTC, and the unmodified `orchestrator_capabilities` result under `capabilities`. Its `reported_at` must be fresh within the shared 30-second operation TTL.

A ready result always carries `assurance: parent-reported`, `launch_guaranteed: false`, `capacity_reserved: false` and `parent_active: unverified`. It proves the snapshot only. It never proves the parent is still active, that worktree preparation will succeed, or that an explicit parent mode ceiling permits the target mode. The later `t3 claim` still applies every gate. Without the envelope, every route-support row is unknown with reason `t3-caller-context-unavailable`.

## task_status is not Brigade acknowledgement

Calling the native `task_status` tool marks the child result delivered to T3. That is T3 delivery acknowledgement, not Brigade consumption. In short, `task_status` acknowledges T3 delivery while Brigade `ack` is separate. Brigade `ack` consumes one exact terminal `result_id` from `t3 observe`. A task can be delivered to T3 and still unacknowledged in Brigade. Finalization requires every task to be terminal, status `completed` and Brigade-acknowledged.

## A lost reply always holds

If `delegate_task` loses its reply, you cannot tell whether T3 created the child. Brigade already consumed launch permission for that attempt. Do not resubmit `delegate_task`, do not change the client request id, and do not launch a replacement thread as a workaround.

Resolve it with `brigade t3 hold-resolve <run> <task> --abandon` when the attempt will never be correlated. The task stays held, non-relaunchable and charged. `hold-resolve --native-json` is the operator assertion path for a resolvable accept conflict, and it never re-grants launch.

## Native target modes and locality

`runtime_mode` and `interaction_mode` default to `inherit`, which omits both from the printed `delegate_task` arguments. T3 accepts `approval-required`, `auto-accept-edits`, `auto` and `full-access` for runtime mode, and `default` or `plan` for interaction mode. A target that still declares `app-server`, or any other mode T3 does not accept, refuses at `t3 open` and at claim with `target-mode-invalid`. Set `inherit`.

A target must run locally: `target_environment` must equal `origin_environment`. A different value refuses with `target-remote-unsupported` at `t3 open` and at claim. Cross-machine governed children are unsupported. Remote editing children belong to `t3_thread_launch` with a worktree workspace strategy, which needs its own design.

`target_project` must equal the T3 project of the parent thread that calls `delegate_task`. T3's local path refuses a different `projectId` with `invalid_request` after the claim already consumed the launch. That is a held lost launch: resolve it with `hold-resolve`, never resubmit. The hold keeps its capacity slot until a reviewed release operation exists.

T3 refuses an explicit runtime or interaction mode broader than the parent thread's. Brigade cannot see the parent ceiling when it prints a claim, so a broader declared mode makes the native call fail after launch permission was consumed. That is a lost launch, not a retry: resolve it with the existing hold and never resubmit.

## Refusals that are expected

- `t3 observe` without a recorded correlation refuses. Only `t3 accepted` or an operator `hold-resolve --native-json` creates correlation.
- `t3 observe` with a different task handle, provider, model, options or parent thread refuses and writes nothing.
- `t3 cancel` refuses once the task is terminal or held, even if an earlier cancel request exists.
- `t3 ack` refuses a nonterminal task, a held task or a mismatched result id.
- `t3 finalize` refuses while an unresolved hold or an abandoned-unknown task exists.
