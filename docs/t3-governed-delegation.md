# Governed native T3 delegation

Status: enrollment, claim, reported correlation and hold resolution are
implemented. No Brigade-side launch, native observation, cancellation or
acknowledgement exists yet.

A T3 Code parent session runs Brigade-governed child tasks by calling Brigade
CLI verbs around its own native `delegate_task` tool. Brigade never calls T3.
The trusted boundary is the Brigade-owned caller and its durable journal.

## Verb sequence

| Verb | State |
| --- | --- |
| `brigade t3 open <batch.json> --target .` | Implemented |
| `brigade t3 claim <run> <task> --parent-thread <thread>` | Implemented |
| `brigade t3 accepted <run> <task> --parent-thread <thread> --native-json <file|->` | Implemented |
| `brigade t3 hold-resolve <run> <task> (--native-json <file|-> | --abandon)` | Implemented |
| `brigade t3 observe <run> <task>` | Later slice |
| `brigade t3 cancel <run> <task>` | Later slice |
| `brigade t3 ack <run> <task>` | Later slice |

`open` validates a `brigade.run_batch.v1` batch, binds every task seat to a
declared native target, retains the exact delegation input, and records
reference-only enrollment facts. It launches nothing and reserves no budget.
`claim`, `accepted` and `hold-resolve` consume a launch claim and record
caller-reported correlation. Later verbs classify native observations, request
cancellation and acknowledge an exact retained result.

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
  before any bytes are trusted.
- On Windows the run directory and each asset are created with a protected
  DACL granting the current user only, bound at creation through
  `work_cmd.nt_private`. The DACL is verified on the held handle before any
  bytes are written, and readback opens the asset once, relative to the held
  run-directory handle, without following a reparse point. An existing object
  with an inherited or widened DACL is refused and never repaired.
- One task batch acceptance.
- One `task.native-enrolled` fact per task carrying only the task ID, the asset
  relative name, the asset SHA-256 digest, the template digest and the
  descriptor version. No prompt text and no raw native JSON enter the journal.
  Reading an asset back verifies its digest and reconstructs the target.

## Call order

The parent session calls one verb per transition and never lets Brigade call
the native tool.

1. `t3 open` retains the exact delegation input and the reference-only
   enrollment facts.
2. `t3 claim` takes the per-invocation run lock, checks the run-wide barrier
   and the asset, template, source and run-state bindings, then makes one
   coordinator call with `reclaim_pending=False`. Inside its
   `record_requested` callback it prepares the attempt, finalizes the
   `NativeIntent` from the retained template, binds it with a digests-only
   journal fact, consumes the launch claim and records the durable dispatch
   request. Only a granted claim prints the delegate arguments, and only after
   every mutation committed. The parent submits that exact input through its
   own `delegate_task` call.
3. `t3 accepted` validates the full returned projection against the durable
   claim and intent and records it as parent-reported correlation. It never
   proves that T3 created or accepted the task.
4. `t3 hold-resolve` records an operator assertion or abandons an attempt that
   will never be correlated. It never re-grants launch.

The claim output names the task text once, the exact target (`provider
instance`, `model`, `options`), `isolation` `worktree`, and the
`clientRequestId` derived from `request_key(run, task, attempt)`. The same
arguments are not printed again for that attempt.

### The lost-reply hold

T3 deduplicates delegate requests only by a command identity that includes an
in-memory per-credential namespace the caller can never see or prove, and the
result does not echo `clientRequestId`. Brigade therefore cannot distinguish a
lost reply from a request that never arrived. It never recommends or
authorizes resubmission. A lost reply always holds the task.

A durable claim without its matching `run.dispatch.requested` fact is a
run-wide barrier. Every new claim in the run refuses until an operator resolves
the pending attempt. A task that was requested but not accepted stays charged,
active and non-relaunchable, and it does not block other tasks. Its dispatch
unit counts against the run budget.

A claim binding without its `task.launch-claimed` fact is the second barrier
state. Brigade does not replay that binding on retry, because a sibling could
already hold the same dispatch ordinal and a replayed identity with a changed
payload would conflict. The run holds every new claim, the task cannot be
correlated, and only `t3 hold-resolve --abandon` closes it. Abandoning this
state charges nothing, because no dispatch request was ever recorded, and the
task stays non-relaunchable.

A requested-but-unaccepted task is exactly that state. It is not an invitation
to retry. Use `t3 hold-resolve --abandon` when the attempt must be closed. It
records the missing dispatch request, which clears the barrier for the rest of
the run, marks the attempt terminal-unknown and leaves the budget unit charged.
The task itself stays held and can never be claimed again.

## Reported correlation, not authenticated acceptance

`t3 accepted` parses the full `delegate_task` result projection, including
`status`, `workState`, the pending-child and pending-question flags, the
terminal metadata and the pending approval shape. It requires a non-null
`childThreadId` and `childRunId`, `childNodeId` equal to the task identity, the
provider instance, model and options exactly equal to the finalized intent, and
the supplied parent thread equal to the claim binding.

Those checks prove consistency with what Brigade already recorded. They do not
authenticate T3. All correlation records are parent-reported, and an operator
assertion is labeled as an assertion. An identical replay is idempotent. A
different native handle for the same task refuses and holds that task. It does
not change the run state.

The projection is then classified by the existing
`native_t3_observations.classify_native_snapshot`, not by its status field
alone. A pending child run, a pending question, a pending approval or a timed
out wait keeps the task nonterminal whatever the status says. A terminal status
also stays nonterminal when the terminal run or result transfer is missing, or
when the terminal evidence contradicts itself. Only a classifier terminal
candidate records a terminal result. A nonterminal record followed by a later
clean terminal projection is the normal path and is accepted.

## Hold resolution

`t3 hold-resolve --native-json` applies the same projection validation as
`t3 accepted`, records the result as an operator assertion, and first records a
missing dispatch request so the run barrier clears and the unit is charged.
It refuses a task that is already held. `--abandon` marks the attempt
terminal-unknown with a durable hold fact. When a launch claim exists without
its dispatch request it records that request, so the unit stays charged. When
no launch claim exists it charges nothing. Neither path ever re-grants launch,
and a later claim refuses.

The verb is available to any principal allowed to mutate the run. Brigade
cannot enforce operator-only access on a shared local account, so the honest
boundary is the filesystem and journal permission of that account.

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

Claim and correlation refusals use the same one-line form. Codes include
`run-missing`, `run-state`, `run-held`, `not-native-run`, `source-dirty`,
`source-changed`, `target-changed`, `asset-custody`, `asset-digest`,
`run-barrier`, `already-claimed`, `task-held`, `capacity-denied`,
`budget-exhausted`, `no-claim`, `claim-incomplete`, `parent-mismatch`,
`intent-mismatch`, `native-invalid`, `native-mismatch`,
`provider-mismatch`, `model-mismatch`, `options-mismatch`,
`accept-conflict` and `observation-conflict`.

## Limits

- Parent-supplied correlation is never authenticated acceptance, and Brigade
  cannot prove that T3 created, started or finished a task.
- Brigade never authorizes resubmission, key rotation or fallback transport
  after a lost reply. The task holds until an operator resolves it.
- `t3 hold-resolve` cannot be restricted to one operator on a shared local
  account.
- A projection that names a linked-environment `remoteChild` is refused,
  because the enrolled target is local. That lane is not supported here.
- The finalized intent keeps the roster target's runtime and interaction modes,
  but the delegate arguments do not send `runtimeMode`, because the declared
  default `app-server` is not one of the native runtime mode literals. The
  result projection does not echo those fields, so acceptance validates
  provider, model and options only.
- Windows is supported with an owner-only DACL that grants the current user
  only. The policy accepts only unflagged allow ACEs for the current token
  user, refuses NULL, inherited, deny and object ACEs, and refuses thread
  impersonation. A platform where `work_cmd.nt_private` cannot bind its API
  still refuses with `unsupported-platform` before any state is written.
- Administrators using backup, restore or take-ownership rights are outside
  this discretionary access boundary. They can read or replace the retained
  input without the DACL refusing them, and Brigade does not claim otherwise.
- T3 deduplicates delegate requests only within one request namespace, and the
  caller can never see or prove that namespace. Brigade does not assume
  cross-session idempotence or authoritative absence.
- Missing or conflicting recovery evidence leaves the obligation held. Brigade
  never refuses resubmission one moment and recommends it the next.
- Direct native calls outside the Brigade path are not governed by these verbs.
- An enrolled native task refuses `prepare_attempt` and `claim_task_launch`
  unless the caller passes the governed flag. Only `brigade t3 claim` does, so
  enrollment cannot be turned into a generic launch grant.
- Enrollment is custody, not admission. Target containment, provider start and
  result acceptance remain separate qualifications.

## Related documents

- [App-hosted design](phase-2046-native-t3-app-hosted-design.md)
- [Governed native child contract](phase-2046-governed-native-child-contract.md)
