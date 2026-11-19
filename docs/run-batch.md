# Explicit task batches

`brigade run batch batch.json --dry-run [--json]` validates a caller-supplied
batch and prints deterministic topological layers, task seats, and a SHA-256
request fingerprint. Dry runs make zero model calls and require no roster.

Without `--dry-run`, the command executes through the existing run orchestrator.
Use `--cwd WORKSPACE` and optionally `--roster ROSTER.toml`. Execution makes no
planner or synthesis call. Remove `synthesis` from this example to execute it.
Validation errors exit 2, with a stable code in text or `error.code` in JSON.
Tasks that `brigade t3 open` refuses as `t3-unsupported` belong here, submitted
as a separate request with its own admission, budget and receipts. See
[work T3 cannot run](t3-governed-delegation.md#work-t3-cannot-run).

```json
{
  "schema": "brigade.run_batch.v1",
  "request_id": "example-request-1",
  "tasks": [
    {"task_id": "inspect", "text": "Inspect the example", "seat": "coder"},
    {"task_id": "review", "text": "Review the findings", "seat": "coder", "depends_on": ["inspect"]}
  ],
  "synthesis": {"text": "Summarize the results", "seat": "writer"}
}
```

Synthesis defaults to off (omit it or use `null`). When present, it must specify
non-empty `text` and a `seat`. It is fingerprinted and displayed as requested,
but never executed. No synthesis budget, receipt, or launch is created yet.
Only the documented fields are accepted. Each task requires `task_id`, `text`,
and `seat`. `depends_on` defaults to an empty list of unique task IDs.
`depends_on` must be a list when present. `null` is refused, unlike
`synthesis`. Each task may also declare `isolation`, either `shared` (the
default) or `worktree`, and `read_only`, a boolean that defaults to false.
Rows are checked in input order, so when several rows are
invalid, the reported code belongs to the first invalid row. A row error names
the row as `tasks[N]` until its `task_id` is valid, and by `task_id` after that.
Selecting a task by admitted capability instead of a named seat, which #2047
also allows, is not part of v1. It remains a later capability-selection slice.

The batch admits 1 through 8 tasks, matching the existing durable assignment
batch bound. Each task or synthesis text is limited to 16,384 UTF-8 bytes.
The CLI reads at most 262,144 input bytes, bounding JSON parsing overhead.
Task IDs and request IDs contain 1 through 128 ASCII characters, starting
with a letter or digit, followed by letters, digits, `.`, `_`, `:`, or `-`.
Seat tokens use the same alphabet with a 512-character limit.

`task_id` is preserved verbatim as the existing `run_tasks.TaskSpec.task_id`
in the lifecycle journal. It uses `run_task_journal.task_id` for
validation. Attempt IDs, run IDs, and provider IDs remain separate lifecycle
identities and are not caller-supplied in this contract. Equal text or equal
seats never merge distinct task IDs. This command does not dispatch ledger
tasks, create task storage, or use the T3 route.

Canonicalization sorts tasks by ID, dependency lists, and object keys, fills
optional dependencies, synthesis, isolation, and read-only defaults, and
preserves exact text.
Layers sort ready task IDs lexicographically. The fingerprint hashes compact
ASCII-escaped canonical JSON, including schema, request ID, texts, seats,
dependencies, non-default isolation and read-only values, synthesis, and any
pinned `base_sha`. Object key, task, and dependency ordering do not
change the fingerprint. Text changes do. Isolation and read-only at their
defaults are omitted from the hashed bytes, so a batch written before these
fields existed keeps its fingerprint. `compare_request(stored_fingerprint,
payload, base_sha=...)` returns `new` for no stored fingerprint, `same` for a
match, and `conflict` otherwise. The caller must look up the stored fingerprint
under the same request ID. A stored value that is not 64 lowercase hex
characters is refused with `invalid-fingerprint`. This
helper provides no storage or resume behavior. The fingerprint is defined by
those exact bytes: Python `json.dumps` with `ensure_ascii=True`, so non-ASCII
text is written as lowercase `\uXXXX` escapes and characters outside the Basic
Multilingual Plane as surrogate pairs. A golden vector in
`tests/test_run_batch.py` pins the bytes and digest for other implementations.

## Isolation and concurrency

A task may be `isolation: "shared"` (the default) or `isolation: "worktree"`.
An unknown value is refused with `invalid-isolation`, and a non-boolean
`read_only` with `invalid-read-only`.

Two or more tasks in the same dependency layer that are both shared and
writable are refused with `shared-writable-concurrency`, which names the task
IDs. Fix it with `isolation: "worktree"`, `read_only: true`, or a `depends_on`
edge that separates the tasks into different layers. A single shared writable
task per layer, and shared writable tasks in different layers, stay allowed.
This preserves today's sequential writable behavior and never weakens the
one-writable-owner rule.

A task that declares `read_only: true` is dispatched through the existing
worker read-only path, so its prompt and CLI invocation carry the same
read-only rules and capability checks as `brigade run --read-only`. A seat that
cannot enforce read-only, including a mutating `codex-cloud` seat, is refused as
an invalid seat, exactly as a read-only run does today. An all-read-only batch
also launches the run itself with `--read-only`, so its receipt records the mode.

Because a read-only task may share a layer with a writer, a batch is stricter
than a plain read-only run: the seat must enforce read-only as `hard` under the
roster sandbox. A soft or prompt-only seat (for example `opencode`, or `codex`
with `sandbox = "workspace-write"`) is refused as an invalid seat. A writable
task under a `read-only` roster sandbox is refused too. The base pin ignores
Brigade's own `.brigade/` state, so the request marker never dirties the base
for a retry.

## Base revision pin

When any task declares `isolation: "worktree"`, the request pins a base
revision at acceptance. The workspace must be a clean git checkout whose HEAD
is a 40-character revision, using the same check the remote T3 transport
applies. A dirty checkout is refused with `base-dirty`, and an unreadable or
non-revision checkout with `base-unavailable`, so uncommitted parent edits are
never implied to reach an isolated child. The dry-run plan and
`batch-request.json` record `base_sha` alongside each task's isolation, and the
fingerprint includes `base_sha`. A retry against a different HEAD is therefore a
`request-conflict`. `plan_batch` and `request_fingerprint` refuse a worktree
request without a base with `base-unpinned`, and a malformed base with
`invalid-base-sha`.

Worktree execution is not implemented yet. Validation and `--dry-run` accept
the field. An execution attempt records the request marker with its pinned base
and each task's isolation, then is refused before any launch with
`isolation-worktree-not-implemented`. Creating the worktrees is a following
slice.

## Per-task worktrees

`brigade.run_batch_workspace` (not wired into execution yet) reserves, creates
and validates one checkout per isolated task:

- The path is always recomputed from the caller repository, run ID and task ID
  as `<worktree-cache>/<repo-slug>-batch-<task-slug>-<digest12>`, where the
  cache is `~/.cache/brigade/worktrees`. A path read from JSON is never
  trusted. Slugged task IDs stay ordinary path components, so a hostile ID
  cannot climb out of the cache.
- A task path must be a direct child of that cache and disjoint from the
  caller checkout, every Git-registered worktree, and every other task's path.
  Overlap refuses with `workspace-overlap`.
- A reservation is exclusive: a pre-existing directory or lease refuses with
  `workspace-exists` and nothing is deleted, adopted, or repaired.
- A created worktree is validated against the reservation: a real directory
  (never a symlink), the same device/inode, the same recomputed path, its own
  Git toplevel, the caller repository's common directory, the bound
  `brigade/batch/...` branch, `HEAD` at the pinned base, and a pristine status
  including ignored files. Any mismatch refuses with `workspace-invalid`.
- A validated binding is recorded in the run journal as digest-only
  `task.workspace-bound` facts (path, branch and device/inode appear only as
  SHA-256 digests). The fact proves custody of a checkout, never launch
  permission: the task stays queued and never attempted.

Isolation separates cooperative workers. It is not a security boundary: a
worker with full filesystem access can still reach the caller checkout by
absolute path, and every worktree shares the repository's Git common
directory. Uncommitted parent edits are never copied either — the worktree
starts at the pinned base revision, so a dirty checkout is refused before a
worker starts.

| Code | Refusal |
| --- | --- |
| `invalid-payload` | Wrong object shape, unknown fields, or invalid text Unicode |
| `unsupported-schema` | Missing or unknown schema/version |
| `invalid-request-id` | Missing or invalid request ID |
| `task-count` | Missing task list or outside 1 through 8 tasks |
| `invalid-task-id` | Missing or malformed task ID |
| `duplicate-task-id` | Repeated task ID |
| `empty-text` | Missing, non-string, empty, or whitespace-only text |
| `text-limit` | Task or synthesis text exceeds its byte bound |
| `invalid-seat` | Missing or malformed seat token |
| `invalid-isolation` | Isolation is neither `shared` nor `worktree` |
| `invalid-read-only` | Read-only is not a boolean |
| `shared-writable-concurrency` | Multiple shared writable tasks in one layer |
| `base-unpinned` | Worktree isolation requested without a pinned base revision |
| `invalid-base-sha` | Base revision is not 40 lowercase hex characters |
| `unexpected-base-sha` | A base was supplied but no task requests worktree isolation |
| `base-dirty` | Worktree isolation requested in a checkout with uncommitted content |
| `base-unavailable` | Worktree isolation requested without a readable 40-character HEAD |
| `isolation-worktree-not-implemented` | Execution requested for a worktree task |
| `invalid-dependencies` | Malformed, duplicate, or excessive dependency list |
| `unknown-dependency` | Reference outside this batch |
| `self-dependency` | Task depends on itself |
| `dependency-cycle` | Dependency cycle, with its closed path in the message |
| `invalid-synthesis` | Synthesis is neither null nor an explicit object |
| `input-read` | Cannot read the input file |
| `input-limit` | File exceeds the input byte bound |
| `invalid-json` | Malformed UTF-8/JSON, excessive nesting, or duplicate object keys |
| `invalid-fingerprint` | Stored fingerprint passed to `compare_request` is malformed |
| `synthesis-not-implemented` | Execution requested with synthesis |
| `request-conflict` | Request ID already reserved with a different fingerprint |
| `request-unknown` | Request reservation has no readable fingerprint, the marker is missing while the journal shows a possible launch, or an accepted request has lost or cannot read its journal |
| `request-busy` | Another caller holds this request’s dispatch lock |


## Execution and retries

The adapter builds assignments directly, with the topological layer as their
stage. Assignment text starts with `Batch task_id: ID`, followed by the exact
caller text. This keeps same-seat tasks distinct in existing assignment IDs,
transport results, and receipts. Task payload digests hash only the exact caller
text bytes. Layers larger than the effective roster `max_workers` split into
consecutive waves through the existing dispatcher. Its fail-fast behavior skips
later waves after a failure, including tasks that do not depend on that failure.
Existing admission, provider limits, model policy, run locks, and seat health
checks still apply. Each selected seat is resolved against the same model-policy
snapshot and receives runtime admission. Selected seats cannot use health or
invalid-final fallbacks. An unusable seat fails visibly. Cached run preferences
and task classification do not select batch seats or modify batch text.
Each task permits at most one worker invocation. Batch dispatch disables
same-seat retries, continuations, and fallback dispatch after the first result.
The first failure is retained as the task result. Planned and `--worker` runs
retain their existing retry policy.

A request reserves `.brigade/runs/batch-<SHA-256 of request ID>/` by atomic
directory creation before dispatch. `batch-request.json` retains its fingerprint
and canonical plan. The directory is rooted at the git worktree root, or the
resolved cwd outside git, using the same root as the run lock.
The lifecycle journal records task acceptance, attempts, observations, and result
references. Retry eligibility and dispatch share a request lock. If no attempt
was prepared and no task may have launched, a matching retry can execute again
after preflight refusal. Once any task may have launched, a matching retry
reports stored states without launching any tasks, including queued, unknown,
or in-flight tasks. Unreadable journals refuse replay, and a different
fingerprint exits 2.
The lifecycle journal, not the marker, is the record of launches, and it is
read on every retry. A missing marker permits a new request while holding the
request lock only when the journal shows no prepared attempt. If the journal
shows a possible launch, a missing marker refuses with `request-unknown`. A
present unreadable or corrupt marker also refuses launch with `request-unknown`.
Once the batch is accepted into the journal, the run directory also records
`batch-journal-enrolled.json`. When that file or any batch result file exists,
the request refuses with `request-unknown` if the journal is absent, unreadable,
or no longer holds the batch, whether or not the marker survives. A missing
journal would otherwise read as "nothing launched" and re-enroll every task.
This holds even if the journal was deleted before any launch. Use a new
`request_id` to start over. A request that never reached acceptance has none of
these files and still retries after a preflight refusal. The guarantee assumes
the journal is append-only: a journal rewound to an earlier valid state that
still contains the accepted batch cannot be told apart from one where nothing
launched.

`brigade runs resume` refuses an explicit batch run directory, because resuming
a provider thread would bypass the per-task launch claim. It recognizes a batch
from the request marker, the enrollment file, or the journal itself, so removing
the marker files alone does not make a batch resumable. Rerun the batch request
instead.

Limit: every fence above lives in the run directory. Deleting several Brigade
files there by hand (for example the journal and the enrollment file after a
provider started but before it wrote a result) leaves no record that a launch
happened, and a rerun of the same `request_id` can launch that task again. This
is the same outcome as deleting the whole run directory. Do not edit or remove
files inside a batch run directory. Use a new `request_id` to start over.
Independently of the marker, each task is checked again at the launch point:
a task the journal shows as attempted or not queued is never invoked again, and
its journal state is left as it was.

When Fleet Hub routing sends a batch seat to another machine, Brigade still
invokes that task once and claims it (`unknown`) first, so the local fence holds.
At-most-once on the remote side then depends on that transport, which #2048
covers for T3.

Each assignment carries its `task_id` and a deterministic `attempt_id`, so the
dispatcher, the result and the live controls name the exact task instead of
reading it back from prompt text. Preparing the attempt happens at the launch
point. The launch claim happens later, inside the run budget reservation for
that exact seat and attempt: the orchestrator passes the assignment into the
reservation, and the batch recorder first calls `claim_task_launch`, then
writes `run.dispatch.requested`. The claim must come first because a binding
that any `run.dispatch.*` event already used is refused. Batches reserve with
`reclaim_pending=False`, so a retry never adopts an earlier pending identity.
Two same-seat tasks in one wave therefore get distinct claims and attempts.

A claim result other than `granted` (`already-claimed`, `terminal`), a binding
conflict, a stale version or any `TaskJournalError` raises `LaunchRefused`.
The dispatcher handles it before route resolution, the model lease and the
provider, records the worker as `refused` (no retry, no fallback seat or route)
and keeps the task's journal state untouched. The budget reservation stays
retained, because a claim may already be durable. A crash after the claim and
before the requested fact leaves the task claimed and unknown, so a retry sees
`already-claimed` and never relaunches. A task is `unknown` only once its claim
is durable, which means `unknown` now implies a granted launch permission.
A prepared attempt with no claim stays `queued` and is still never relaunched.

Transports that expose a documented provider value fill `provider_task_id` on
the worker result, and the batch records it on the task observation. Today only
codex-cloud does, through its task ID. Original run and result IDs stay empty
until a transport documents them.

JSON output uses `brigade.run_batch_result.v1`, with `run_id`, `request_id`, `run_status`, and a
`tasks` object keyed by task ID. Each member has `status`, `not_run`, `result_id`, and
`result_ref` (relative to the run directory, or null). Completed results persist
as each worker finishes, as the same sanitized projection the run receipt uses
(no raw stdout or stderr captures). Failed workers retain their result receipt too. Skipped
tasks remain queued in the journal, have `not_run: true`, and have no result reference. Cancellation
and timeout preserve completed results and report unresolved tasks as stored.
Exit 0 requires every task to be completed successfully and the run to pass
finalization. Worker failures, cancellation, and timeout exit 1. Preflight and
run-guard refusals retain their nonzero dispatch exit code and stderr reason.
Batch runs retain normal drift checkpoints, worker-result artifacts, final
receipts, artifact collection, and lineage metadata without calling synthesis.
Synthesis execution exits 2 with `synthesis-not-implemented`. Dry run still shows
its requested seat. No synthesis model call or budget is created.

See [kinds of delegated work](execution-model.md#kinds-of-delegated-work) for how
batch tasks compare with ledger tasks, run-lineage children, remote fleet
requests, and T3 app-owned child tasks.
