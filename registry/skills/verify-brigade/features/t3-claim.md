---
status: active
proof: drive
surfaces: t3 claim, t3 accepted, t3 hold-resolve
drive: t3-enrollment
last-drive: 2026-11-17 6fe85abd6
---

# Governed native T3 claim and correlation (`brigade t3 claim`)

`brigade t3 claim` consumes one launch permission, prints the exact
`delegate_task` arguments once after the durable commit, and never calls T3.
`brigade t3 accepted` records a returned projection as parent-reported
correlation. `brigade t3 hold-resolve` records an operator assertion or
abandons an attempt. None of them resubmits a lost reply.

## Sub-features

- Two tasks on one seat get distinct attempts, intent digests and client
  request keys. The claim output names the task text, the exact target
  (provider instance, model, options), `isolation` `worktree` and the
  `clientRequestId`.
- A second claim of the same task refuses with `already-claimed`.
- An identical `accepted` replay is idempotent and appends no journal line.
  The record is labeled `parent-reported`, never authenticated acceptance.
- `hold-resolve --abandon` records `abandoned-unknown`, and a later claim
  refuses with `task-held`.
- The lifecycle journal never contains task text.

## How to get to it (user POV)

A parent session enrolls a batch with `brigade t3 open`, then runs:

```bash
brigade t3 claim <run_id> <task_id> --parent-thread <thread>
brigade t3 accepted <run_id> <task_id> --parent-thread <thread> --native-json result.json
brigade t3 hold-resolve <run_id> <task_id> --abandon
```

It submits the printed `delegate_task` arguments with its own native tool and
keeps the returned projection for `accepted`. `claim` is idempotent only in
the sense that it never grants twice. A lost reply holds the task.

## Driving it with control-brigade

```bash
C=registry/skills/verify-brigade/control-brigade.py
TARGET=$($C new-target | python3 -c 'import json,sys; print(json.load(sys.stdin)["target"])')
$C t3-enrollment --target "$TARGET"
$C evidence --target "$TARGET" --label t3-claim
$C cleanup
```

The same drive enrolls a two-task batch, claims both tasks, replays one claim,
accepts task one twice with the same projection, and abandons task two. Observed
on the drive that owns this file: `ok: true`, distinct `clientRequestId` values,
`already-claimed` on the replay, `parent-reported` with `replayed: true` on the
second accept, `abandoned-unknown`, `task-held` on the later claim, and task
text absent from the lifecycle journal.

## Gotchas

- The run lock is per invocation with a zero wait. A concurrent verb refuses
  with `run-busy` rather than waiting.
- A durable claim without its `run.dispatch.requested` fact refuses every new
  claim in the run with `run-barrier`. `hold-resolve --abandon` records the
  missing request, which frees the run while the task stays held. A requested
  but unaccepted task does not block siblings, but it keeps its budget unit
  charged.
- A claim binding without its launch claim is the same barrier. Brigade never
  replays it. `hold-resolve --abandon` closes it and charges nothing.
- Every claim re-reads every retained asset and refuses on a digest, custody,
  mode or source revision mismatch.
- `accepted` refuses a mismatched provider, model, options, parent thread or
  child node, and a null child thread or run. A different handle holds the
  task, not the run.
- `hold-resolve --native-json` refuses a task that is already held. Abandon is
  the only resolution for an abandoned task.
- `hold-resolve` cannot be restricted to one operator on a shared local
  account. It never re-grants launch.
- POSIX-only until an owner-only asset DACL exists on Windows.
