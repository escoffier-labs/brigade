# Explicit task batches

`brigade run batch batch.json --dry-run [--json]` validates a caller-supplied
batch and prints deterministic topological layers, task seats, and a SHA-256
request fingerprint. Dry runs make zero model calls and require no roster.

Without `--dry-run`, the command executes through the existing run orchestrator.
Use `--cwd WORKSPACE` and optionally `--roster ROSTER.toml`. Execution makes no
planner or synthesis call. Remove `synthesis` from this example to execute it.
Validation errors exit 2, with a stable code in text or `error.code` in JSON.

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
`synthesis`. Rows are checked in input order, so when several rows are
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
optional dependencies and synthesis defaults, and preserves exact text.
Layers sort ready task IDs lexicographically. The fingerprint hashes compact
ASCII-escaped canonical JSON, including schema, request ID, texts, seats,
dependencies, and synthesis. Object key, task, and dependency ordering do not
change the fingerprint. Text changes do. `compare_request(stored_fingerprint,
payload)` returns `new` for no stored fingerprint, `same` for a match, and
`conflict` otherwise. The caller must look up the stored fingerprint under the
same request ID. A stored value that is not 64 lowercase hex characters is
refused with `invalid-fingerprint`. This
helper provides no storage or resume behavior. The fingerprint is defined by
those exact bytes: Python `json.dumps` with `ensure_ascii=True`, so non-ASCII
text is written as lowercase `\uXXXX` escapes and characters outside the Basic
Multilingual Plane as surrogate pairs. A golden vector in
`tests/test_run_batch.py` pins the bytes and digest for other implementations.

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
| `request-unknown` | Request reservation has no readable fingerprint, or the marker is missing while the journal shows a possible launch |
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
Independently of the marker, each task is checked again at the launch point:
a task the journal shows as attempted or not queued is never invoked again, and
its journal state is left as it was.

When Fleet Hub routing sends a batch seat to another machine, Brigade still
invokes that task once and marks it `unknown` first, so the local fence holds.
At-most-once on the remote side then depends on that transport, which #2048
covers for T3.

Task launch claims remain deferred: the existing dispatch callback binds budget
reservations to a seat and attempt, without exposing an exact task candidate to
`claim_task_launch`. The request reservation prevents replay launches instead.
A task becomes unknown before its prompt is built, so this state can include a
launch that was never accepted. It is never retried automatically.

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
