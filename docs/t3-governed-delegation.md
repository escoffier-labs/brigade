# Governed native T3 delegation

Status: enrollment slice implemented. No launch, acceptance or budget
reservation exists yet.

A T3 Code parent session runs Brigade-governed child tasks by calling Brigade
CLI verbs around its own native `delegate_task` tool. Brigade never calls T3.
The trusted boundary is the Brigade-owned caller and its durable journal.

## Verb sequence

| Verb | State |
| --- | --- |
| `brigade t3 open <batch.json> --target .` | Implemented |
| `brigade t3 claim <run> <task>` | Later slice |
| `brigade t3 accepted <run> <task>` | Later slice |
| `brigade t3 observe <run> <task>` | Later slice |
| `brigade t3 cancel <run> <task>` | Later slice |
| `brigade t3 ack <run> <task>` | Later slice |

`open` validates a `brigade.run_batch.v1` batch, binds every task seat to a
declared native target, retains the exact delegation input, and records
reference-only enrollment facts. It launches nothing and reserves no budget.
The later verbs consume a launch claim, retain native acceptance evidence,
classify observations, request cancellation, and acknowledge an exact retained
result.

## Seat bindings

A seat is eligible only when the roster declares a native target for it.
Declarations grant no readiness, admission or launch authority.

```toml
[t3_targets.coder]
origin_environment = "env-origin"
origin_thread = "thread-parent"
target_environment = "env-target"
target_project = "project-example"
provider_instance = "codex-example"
driver = "codex"
model = "example-model"
isolation = "worktree"

[t3_targets.coder.options]
reasoningEffort = "high"
```

The eight identity fields above are required. `runtime_mode` defaults to
`app-server` and `interaction_mode` to `default`. `isolation` must be
`worktree`, unknown fields refuse the whole batch, and a seat without a
declaration refuses the whole batch. The source revision is never taken from
the roster. It is the clean workspace `HEAD` at `open` time, and a dirty
workspace refuses.

## What open writes

- A run directory named from the request identity, reported as `run_id`, and
  created owner-only (`0700`).
- `run.json` with status `awaiting-native-parent` and the lifecycle journal
  requested.
- A request marker holding the batch fingerprint and the source revision.
- One atomic asset per task holding the exact retained delegation input: task
  text and the selected target. Each asset is created with exclusive no-follow
  creation in owner-only mode (`0600` on POSIX), and the write never replaces
  an existing file. Publish replay and readback bind the run directory
  descriptor-relative and require a single-link regular file with mode `0600`
  before any bytes are trusted. Windows is refused because its asset would
  inherit the parent directory ACL instead of an owner-only DACL.
- One task batch acceptance.
- One `task.native-enrolled` fact per task carrying only the task ID, the asset
  relative name, the asset SHA-256 digest, the template digest and the
  descriptor version. No prompt text and no raw native JSON enter the journal.
  Reading an asset back verifies its digest and reconstructs the target.

## Ownership between verbs

Each verb acquires `runguard.run_lock` for the exact run with a zero wait and
`stale_action="claim"`, performs its journal work, and releases. No owner token
is persisted and no long-lived owner process is imitated. Every journal
mutation remains its own transaction.

A run whose status is `awaiting-native-parent` normally has no live lock holder
between verbs. That is the externally controlled state, not a stale or
resumable run. `runs show` reports it as non-terminal, the reaper skips it and
never terminalizes it, patrol classifies it as waiting, and `brigade runs
resume` and `brigade runs recover` both refuse it. A dead holder lock is
reclaimed by the next verb's own claim without rewriting the receipt as
failed.

## Idempotency and refusals

Opening the same request again is idempotent when the batch fingerprint and the
workspace source revision both match. Brigade replays the same journal facts
and reports the same `run_id`. Any other reuse of that request ID refuses with
`request-conflict` and writes nothing.

Every task must declare `worktree` isolation, and a batch with synthesis
refuses, because native enrollment has no local shared checkout and no
synthesis step.

`t3 open` prints JSON with `schema`, `run_id` and one entry per task holding
only `task_id`, `seat`, `state` and the target summary (`provider_instance`,
`model`). Refusals print one bounded `error [<code>]: <diagnostic>` line to
stderr and exit non-zero. Refusal codes include `source-dirty`,
`roster-missing`, `unknown-seat`, `seat-unbound`, `invalid-target`,
`target-isolation`, `isolation-required`, `synthesis-unsupported`, `run-busy`,
`request-conflict`, `asset-conflict`, `asset-custody`, `asset-digest`,
`unsupported-platform` and `journal-refused`.

## Limits

- Windows is refused for now. `brigade t3 open` exits with
  `unsupported-platform` before any state is written, and asset readback
  refuses too, because an asset there would inherit the parent directory ACL
  instead of an owner-only DACL. An owner-only DACL is not implemented or
  verified in this slice.
- Parent-supplied native JSON is caller-supplied evidence, not authenticated
  acceptance. Brigade cannot prove that T3 accepted a delegate request from a
  returned payload alone.
- T3 deduplicates delegate requests only within one request namespace. Brigade
  does not assume cross-session idempotence or authoritative absence.
- Missing or conflicting recovery evidence leaves the obligation held. Later
  slices refuse resubmission, key rotation and fallback transport.
- Direct native calls outside the Brigade path are not governed by these verbs.
- An enrolled task refuses `prepare_attempt` and `claim_task_launch` unless the
  caller is the governed native path. No caller in this slice passes that flag,
  so enrollment cannot be turned into a generic launch grant.
- Enrollment is custody, not admission. Target containment, provider start and
  result acceptance remain separate qualifications.

## Related documents

- [App-hosted design](phase-2046-native-t3-app-hosted-design.md)
- [Governed native child contract](phase-2046-governed-native-child-contract.md)
