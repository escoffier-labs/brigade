---
status: active
proof: drive
surfaces: t3 observe, t3 cancel, t3 ack, t3 finalize, t3 status
drive: t3-enrollment
---

# Governed native T3 lifecycle (`brigade t3 observe`)

`brigade t3 observe` records a supplied `task_status` projection against an
existing correlation. `brigade t3 cancel` records a digests-only cancel
request and prints the native `task_cancel` arguments. `brigade t3 ack`
consumes one exact terminal result. `brigade t3 finalize` closes the run
explicitly. `brigade t3 status` reports fixed per-task tokens and never task
text, option values, native payloads or child ids.

## Sub-features

- `observe` refuses without a correlation and refuses a mismatched handle,
  provider, model, options or parent thread with no journal line.
- A nonterminal observation then a clean terminal observation both record. The
  terminal output carries a `result_id`, and an identical replay appends
  nothing.
- The terminal result artifact digest covers the handle, status, terminal run,
  selected result transfer, pending flags, work state and quiescence. Two
  different terminal projections produce different result ids.
- `cancel` returns the same arguments on replay with one durable fact. It
  refuses a terminal or held task with empty stdout.
- `ack` refuses a nonterminal task, a held task or a wrong result id, and is
  idempotent for the exact terminal result.
- `finalize` writes `completed` only when every task has terminal status
  `completed`, a result id and an acknowledgement, otherwise `failed`. It
  refuses an unresolved hold and an abandoned-unknown task. `status` and an
  idempotent `ack` still work after finalization.
- `hold-resolve --native-json` clears a resolvable accept-conflict hold after
  the same validation as `accepted`, records a closed resolution fact, and
  never re-grants launch. `abandoned-unknown` stays permanent.
- New `task.native-correlated` events carry no `child_thread_id` or
  `child_run_id`. Those fields remain optional legacy input.

## How to get to it (user POV)

After `brigade t3 open` and `brigade t3 claim`, a parent session runs:

```bash
brigade t3 accepted <run> <task> --parent-thread <thread> --native-json result.json
brigade t3 observe <run> <task> --parent-thread <thread> --native-json status.json
brigade t3 cancel <run> <task> --parent-thread <thread>
brigade t3 ack <run> <task> --result-id <id>
brigade t3 finalize <run>
brigade t3 status <run> --json
```

Every verb accepts `--target`. `observe` and `cancel` require
`--parent-thread`. `observe` and `accepted` take `--native-json`, `ack` takes
`--result-id`, and `status` takes `--json`.

## Driving it with control-brigade

```bash
C=registry/skills/verify-brigade/control-brigade.py
TARGET=$($C new-target | python3 -c 'import json,sys; print(json.load(sys.stdin)["target"])')
$C t3-enrollment --target "$TARGET"
$C evidence --target "$TARGET" --label t3-lifecycle
$C cleanup
```

The shared enrollment drive claims both tasks, accepts and observes task one
to a terminal result, acknowledges it, accepts task two, cancels it twice,
abandons it, then runs finalize and status. It then opens a second one-task run,
completes and acknowledges it, and finalizes it as `completed`. It asserts one
cancel fact, identical cancel arguments on replay, no child ids in correlation
events, task text absent from the journal, the abandoned refusal, and the
successful completed closeout.

## Gotchas

- `observe` never creates a correlation. Without `accepted` or an operator
  `hold-resolve --native-json` it refuses with `no-correlation`.
- `cancel` is a request, not proof of native cancellation or containment.
- A held task refuses both `cancel` and `ack` until an operator resolves it.
- `finalize` refuses an abandoned-unknown task rather than release live-work
  capacity. The dispatch unit is never refunded.
- `finalize` is explicit. Nothing else closes the run.
