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

Worktree execution is wired: each isolated task runs in its own reserved
checkout, described in [Per-task worktrees](#per-task-worktrees). Execution
checks the pin again before it launches anything. When the caller workspace's
clean source revision no longer equals `base_sha`, whether HEAD moved or content
appeared since acceptance, the whole batch is refused with `source-changed` and
no worker starts. Brigade's own `.brigade/` state is ignored by that check, so
the request marker never dirties the base.

## Per-task worktrees

`brigade.run_batch_workspace` reserves, creates and validates one checkout per
isolated task, and batch execution uses it before any worker is submitted:

- Before any worker is submitted, and in plan order, each isolated task is
  prepared. A task whose journal already records a binding replays it: the path
  is recomputed, the binding is rebuilt, the digests must match, and the
  checkout is validated before reuse. A task without one is checked for overlap,
  reserved, created at the pinned base, validated, and recorded as a
  `task.workspace-bound` fact. A task whose journal already shows an attempt is
  left to the existing relaunch fence and gets no checkout at all.
- Immediately before each launch the checkout is validated again, including a
  pristine status. A file that appeared between creation and launch refuses that
  task.
- A workspace refusal refuses only its own task: the closed code is reported at
  the launch fence, its journal state is untouched, and nothing launches for it.
  No refusal deletes or repairs anything.
- Each task checkout holds its own run lock from creation through worker exit,
  so `brigade runs prune-worktrees` skips a live task worktree. A `brigade` run
  started inside a task checkout while the batch holds that lock waits or
  refuses it like any other live run.
- The worker process runs in its task checkout. Cassettes, receipts, the task
  journal, the run budget coordinator and caller run-lock resolution stay on the
  caller workspace, and so do the run-level HEAD and drift checks.
- Task checkouts and their `brigade/batch/...` branches are kept after the run,
  on success, failure, cancellation and crash. Nothing in this path removes a
  checkout or a branch. A later `brigade runs prune-worktrees` reads Git status
  without ignored files, so a task checkout holding only ignored files can look
  clean to it.
- Fleet Hub remote routing hands the task text to another machine, which cannot
  see a checkout reserved on this one. A batch that declares worktree isolation
  and would route remotely is refused with `isolation-remote-unsupported`
  before any launch.

The path, reservation and validation rules are unchanged:

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

Isolation separates cooperative workers. It is not a security boundary. A
worker with full filesystem access can still reach the caller checkout by
absolute path, and every worktree shares the repository's Git common directory.
Only a seat confined to its own folder — a `codex` seat on the
`workspace-write` sandbox — is actually restricted to the task checkout; every
other writable seat can write outside it. Uncommitted parent edits are never
copied either: the worktree starts at the pinned base revision, so a dirty
checkout is refused before a worker starts.

Known limits:

- Resuming an approval pause for an app-server worker in a task worktree is not
  yet correlated: the request is bound in the task checkout, while the resume
  handoff reads the caller workspace.
- An enrolled Fleet session bootstrap binds its launch receipt to the caller
  checkout, which shares the same repository.
- A batch with any remote route refuses all worktree tasks
  (`isolation-remote-unsupported`).
- Repository-configured clean or process filters can still run during `git add`
  when collection commits a worker's leftovers, consistent with workers being
  cooperative rather than adversarial. Clean filters configured in a task
  worktree can also run during `git status` and `git worktree remove`, so
  disposal reads a checkout's state with the same trust it extends to the
  worker.

## Task artifacts

Successful execution and successful artifact delivery are separate facts. A
worker finishing says only that it finished. After every terminal worker result,
successful or failed, and while that task's own run lock is still held,
`brigade.run_batch_artifacts` collects what the worker left in its reserved
checkout. A failed task's work is collected too, so it stays recoverable.

Collection checks identity first: the checkout must still be the reserved one,
on its bound `brigade/batch/...` branch, with the same toplevel and Git common
directory. Only the `HEAD`-at-base comparison is relaxed, because the worker may
have committed on its branch. A checkout that fails that check is recorded as
`collection-failed` and nothing is touched.

Collection then produces a candidate commit, and never deletes, resets, cleans
or checks anything out:

- If anything tracked or untracked (but not ignored) changed, collection runs
  `git add -A` and commits with the fixed identity `Brigade <brigade@localhost>`,
  the message "brigade batch task <task_id>", and no hooks. The identity is
  pinned through the environment, so the caller's Git config is never used.
  The candidate is then the checkout's `HEAD`.
- If the worker committed and left nothing behind, its own commit is the
  candidate.
- If `HEAD` is still the pinned base and nothing changed, there is no candidate
  and the delivery is `no-changes`.

A candidate must descend from the pinned base (`git merge-base --is-ancestor`).
When it does, collection writes a bounded patch, `git diff --binary <base>
<candidate>`, so it applies to a base checkout. A candidate that does not
descend is recorded as `base-diverged` with the commit and no patch: a diff
against an unrelated history is not a result of this base.

Ignored files are counted, not collected. They are left exactly where the worker
put them, they are never committed, and they never appear in the patch.

| Delivery | Meaning |
| --- | --- |
| `retained` | A candidate commit exists and is kept on the task branch |
| `no-changes` | `HEAD` is the pinned base and nothing changed; no candidate |
| `base-diverged` | The candidate does not descend from the pinned base; the commit is recorded, no patch is written |
| `collection-failed` | The checkout could not be collected; a closed reason says why and nothing was changed |

Each worktree task gets one sidecar, `<run_dir>/batch-artifact-<sha256 of task
id>.json`, with the schema in
[receipt schemas](receipt-schemas.md#brigadebatch_task_artifactv1). It records
the execution state, the delivery, the base, the branch, the candidate
(if any), the patch reference, digest and size, the changed-path count and the
ignored-file count. It contains no absolute paths, file contents, or diff text.
The patch itself is `<run_dir>/batch-artifact-<sha256 of task id>.patch`, created
exclusively with mode `0600`.

Patches are local-only. A patch can carry the contents of untracked files a
worker created, so `batch-artifact-*` files are excluded from every
`brigade runs export` and archive surface. Copy a patch out yourself if you need
one, and treat it as sensitive.

A patch is capped at 32 MiB. Git's diff is streamed through a bounded pipe and
stopped as soon as the next chunk would pass the cap, so an oversize diff is
never written to disk in full: the partial file is removed and the sidecar
records `patch-oversize` as its reason. A diff that times out is recorded the
same way with reason `patch-timeout`. Either way the candidate commit on the
task branch is still the retained copy. Nothing in this path removes a
checkout, a branch or a commit. Applying a candidate to a base is the explicit
[integration](#integration) step, and removing the checkouts is the explicit
[disposal](#disposal) step.

Collecting the same attempt again — a crash after the patch was written but
before the observation, or a repeated observe — reuses the sidecar already on
disk when it still describes what is there: the same attempt, base and branch,
a candidate equal to the branch's current `HEAD`, and a recorded patch whose
size and digest match. A patch left behind without its sidecar is re-adopted
only when streaming the same diff reproduces its exact size and digest. A
sidecar that no longer matches is never overwritten: the delivery is
`collection-failed` with reason `artifact-conflict`, and nothing is touched.

The journal links the artifact: `task.observed` uses `artifact_kind="commit"`
and the artifact sidecar's SHA-256 as `artifact_digest` when a candidate exists,
and keeps `artifact_kind="receipt"` otherwise. The sanitized result receipt is
written either way. `brigade runs tasks show` reports both sidecar references
and digests.

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
| `source-changed` | The caller workspace no longer matches the pinned base when execution starts |
| `isolation-remote-unsupported` | Worktree tasks in a batch that Fleet Hub routing would run on another machine |
| `workspace-overlap` | A task checkout would overlap the caller repository, another worktree, or another task |
| `workspace-exists` | A task checkout path or reservation already exists |
| `workspace-invalid` | A task checkout is missing, dirty, or not the reserved checkout |
| `workspace-unavailable` | A task checkout or its Git state could not be read or created |
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


## Integration

A retained candidate is a result, not an applied change. Integration is a
separate, explicit command:

```bash
brigade runs tasks integrate <run> <task_id>... [--check] [--json]
```

It merges the named tasks' retained candidate commits, in the order given, into
one new commit and creates the branch `brigade/integrate/<integration_id>`. The
identity is the first 16 hex digits of a SHA-256 over the run ID, the ordered
task IDs and the pinned base, so the same selection always names the same ref
and a different order names a different one. The record keeps that order, and
so does every `task.integrated` fact.

Nothing else happens. The caller's branch, `HEAD`, index and working tree are
never touched or checked out, and Brigade never merges the new ref into another
branch, pushes it, or publishes it. Inspect it with `git log <ref>` and take it
yourself with `git merge <ref>`; the human output names both commands.

`--check` runs every precondition and every quarantined merge, then stops: it
writes no ref, no record and no journal fact. The merges it runs still leave
unreachable objects in the object store, which a normal `git gc` removes. The
command holds the caller workspace's run lock for its whole duration in either
mode, so a live batch refuses it with `target-busy`.

Preconditions are checked before anything is written. Every named task must be
terminal, unique, and accepted by the run. Its artifact sidecar must exist, be a
regular bounded file that is not a symlink, hash to the `task.observed`
`artifact_digest` with `artifact_kind` `commit`, and record `retained` delivery
with a candidate commit. The sidecar's branch must be the recomputed
`brigade/batch/...` branch, and its `base_sha` must be the run's pinned base. The
task branch tip in the caller repository must still equal the candidate, the
candidate must descend from the pinned base, and every selected task must share
that base.

### Quarantined merge

A repository can define a merge driver in its config, and a tracked
`.gitattributes` can name one. A worker can write either, so every merge runs in
a temporary bare repository created under the batch run directory, with its own
empty config. With no driver definition to resolve, any attribute naming one
falls back to Git's builtin text merge, and with no hook path no hook can run.
`merge-tree --write-tree --name-only -z` (Git 2.38 or newer) merges each
candidate into the running tree in order; a conflict in any step refuses the
whole integration and names the tasks merged so far and at most 50 conflicted
paths, decoded leniently, so a path that is not valid UTF-8 still reports as a
conflict. Nothing is partially integrated.

Merges read and write the caller repository's object store directly through
`GIT_OBJECT_DIRECTORY`, so the final commit is already in place when the merge
finishes. Object transfer from quarantine uses no Git transport at all: no
remote, no URL, and no transport or credential helper is resolved, so
`url.*.insteadOf` rewrites, `core.sshCommand` and `core.fsmonitor` in the
caller's config cannot run, and hooks stay pinned to the null device. The
caller's branch is then created with an absent-ref compare-and-swap
(`git update-ref <ref> <commit> <zero>`), so an existing ref is never moved or
replaced. The quarantine directory is removed afterwards; unreachable objects
from a refused or superseded integration stay in the object store and are
cleaned up by a normal `git gc`.

Every commit Brigade writes here is stamped with the pinned base commit's
timestamp rather than the current time, so the same selection over the same base
always produces the same commit. That is what makes a crash repairable.

### Recording and recovery

The run directory gets one exclusive
`batch-integration-<integration_id>.json` record, written *before* the ref is
created, then the ref, then one `task.integrated` fact per task, in order.
Rerunning the same command is the repair path for either crash window: a record
whose ref is missing creates the ref, a ref that already holds the expected
deterministic commit is recognized as this integration's own ref and gets its
record and facts, and an already complete integration reports
`already-integrated` and writes nothing. A record that does not match the
selection, or a ref that later holds another commit, refuses with
`ref-conflict` instead of rewriting anything.

### Refusal codes

| Code | Refusal |
| --- | --- |
| `no-task-journal` | The run directory has no task journal |
| `journal-unreadable` | The journal, or the fact append, could not be read or written |
| `unknown-run` | No readable batch request marker, or no pinned base revision |
| `unknown-task` | A named task is not accepted by this run |
| `duplicate-task` | A task id is named more than once |
| `task-not-terminal` | A named task has no terminal result |
| `artifact-mismatch` | The artifact sidecar is missing, unreadable, altered, or names another task, branch or kind |
| `no-candidate` | The artifact delivery is not `retained`, or it holds no candidate commit |
| `candidate-moved` | The task branch tip no longer equals the recorded candidate |
| `base-diverged` | The candidate does not descend from the pinned base |
| `base-mismatch` | A selected task was collected against another base |
| `target-busy` | Another caller holds the workspace run lock |
| `git-too-old` | Git is older than 2.38, so it has no `merge-tree --write-tree` |
| `conflict` | A merge conflicts; the refusal names the tasks and paths |
| `ref-exists` | The result ref already exists and no record authorizes it |
| `ref-conflict` | A recorded or expected ref now holds another commit |
| `record-conflict` | An existing integration record does not match this selection |
| `git-failed` | A Git read or write, or the quarantine cleanup, failed |

JSON output uses `brigade.runs_tasks_integrate.v1`, with `run_id`,
`request_id`, `check`, `status` (`created`, `checked`, `already-integrated`),
`base_sha`, `integration_id`, `ref`, `integration_commit`, `task_ids`,
`candidate_commits`, and `refusal` (`null` on success). Human refusals print
`error: <code>: <detail>` to stderr and exit 2.


## Disposal

Removing a per-task worktree is the last explicit step, never a side effect:

```bash
brigade runs tasks dispose <run> <task_id>... [--confirm] [--discard <task_id>]... [--delete-branch] [--json]
```

Without `--confirm` it prints what it would do and changes nothing at all: no
lock, no fact, no removal. With `--confirm` it works through the named tasks in
order, and one refused task never stops the next.

The security rule is one sentence: **deleting a worktree must never lose the
only copy of work.** Every check below is a refusal that leaves the task
exactly as it was.

1. **Locks.** With `--confirm`, the caller workspace's run lock is held for the
   whole command (`target-busy`), and each task's own run lock is acquired and
   held from its first check through removal and recording (`task-busy`). There
   is no check-then-act gap. A dry run takes no lock: it reads the lock state and
   refuses a live, foreign or unreadable one with the same code, without
   changing it.
2. **Identity.** The task must be terminal in the journal (`not-terminal`) and
   carry a `task.workspace-bound` fact. The binding is rebuilt from the journal
   — the path and branch are recomputed, never read back from a file — and must
   match its recorded digests, device/inode, Git toplevel and common directory
   (`workspace-invalid`).
3. **A retained copy.** Exactly one of:
   - *integrated*: a `task.integrated` fact whose ref
     `refs/heads/brigade/integrate/<id>` still holds the recorded
     `integration_commit`;
   - *retained*: the artifact sidecar still hashes to the digest the journal
     recorded, its delivery is `retained`, and the task branch tip is still its
     candidate commit;
   - *no-changes*: the sidecar delivery is `no-changes`, the receipt digest
     matches the observation, and the checkout is unchanged.

   Otherwise the task is refused with `not-retained` unless `--discard` names it.
   In every non-discard case the task branch tip must still equal the recorded
   candidate commit (or the pinned base for `no-changes`), or the task is
   refused with `branch-moved`.
4. **Ignored files.** `git status --porcelain --ignored --untracked-files=normal`
   must show no `!!` entries. Ignored files are never collected, so removing the
   checkout would delete the only copy; the refusal is `ignored-files` unless
   `--discard` names the task.
5. **Removal.** A retained or integrated checkout must show no other uncommitted
   entry either, because that work is on no branch: the refusal is
   `remove-refused`, and a dry run reports it too. Then `git worktree remove
   <path>` runs through the hardened runner in the caller repository, **without
   `--force`**. A checkout Git still refuses to remove is refused with
   `remove-refused` and left untouched. Brigade never deletes a worktree
   directory itself.
6. **Recording.** As soon as the removal succeeded, and while the task lock is
   still held, one closed `task.disposed` fact records the mode and whether the
   branch survived. The fact is written *before* the optional branch delete, so a
   failed delete can never leave the checkout gone with no recorded disposal.
   Only after that is the reservation lease (`<cache root>/.run-locks/<name>`)
   released; it is released on a branch-delete refusal too, because the disposal
   is already final. Only an empty directory is removed, so a live lock, a
   pending stale claim or a recovery gate keeps it.

`--discard <task_id>` is the caller's explicit disposal decision. The task must
also be named in the command. It is the only path that skips the retained-copy
proof, tolerates ignored files, and lets Git force the removal. Recorded mode:
`discarded`.

`--delete-branch` deletes the task branch only for an *integrated* task, and only
after proving the integration commit is still reachable from its integration
ref. The delete is a compare-and-swap on the candidate
(`git update-ref -d refs/heads/<branch> <candidate>`), so a moved branch is never
deleted. The proof uses the integration record whose ref still holds its
recorded commit, not simply the newest one: an older integration that still owns
the work is the copy that counts. Every other task keeps its branch, and an
integration ref is never deleted.

A refused branch delete does not undo the removal. The `task.disposed` fact is
already recorded with `branch_kept: "kept"`, and the command reports the refusal
(`branch-moved` or `git-failed`) after it. Because the branch delete is a
separate, idempotent step, running the same command with `--delete-branch`
again retries only that delete, with the same compare-and-swap and reachability
proof, and reports `already-disposed` with `branch_kept: "deleted"` on success.

### Crash repair

The fact is appended while the task lock is still held, so a crash between
removal and the fact leaves the checkout gone and the binding present. Rerunning
the same command repairs that state instead of failing forever: it sees the
missing path, re-proves the retained copy from the journal, records the missing
`task.disposed` fact with the proof it found, and releases the stranded lease.
The same is true of `--delete-branch`: an already deleted branch is recognized
once the integration ref still holds the recorded commit.

Rerunning dispose on a task that already has a `task.disposed` fact reports
`already-disposed` and writes no journal fact. With `--confirm` it also takes
the task lock, releases the reservation lease a crash can leave behind, and,
with `--delete-branch`, retries only the branch delete. A dry run does neither:
it takes no lock at all and reads the lock state without changing it.

### Refusal codes

| Code | Refusal |
| --- | --- |
| `no-task-journal` | The run directory has no task journal |
| `journal-unreadable` | The journal could not be read, or the fact could not be written |
| `unknown-run` | The run directory is not a run, or has no readable batch request marker |
| `unknown-task` | A named task is not accepted by this run, or `--discard` names one that is not |
| `duplicate-task` | A task id is named more than once |
| `target-busy` | Another caller holds the workspace run lock |
| `task-busy` | Another caller holds the task's run lock |
| `not-terminal` | The task has no terminal result |
| `workspace-invalid` | The binding does not match, or the checkout is not the reserved one |
| `workspace-unavailable` | The checkout or its Git state could not be read |
| `not-retained` | No retained copy, or an unchanged checkout is not unchanged |
| `branch-moved` | The task branch tip is not the recorded revision |
| `ignored-files` | The checkout still holds ignored files, which are never collected |
| `remove-refused` | Git refused to remove the checkout; nothing was changed |
| `artifact-mismatch` | The journal records two conflicting artifact digests |
| `git-failed` | A Git read or write failed |

JSON output uses `brigade.runs_tasks_dispose.v1`, with `run_id`, `confirm`,
`delete_branch`, `discard`, `refusal` (`null` unless the whole command was
refused), and `tasks`: one row per named task with `task_id`, `status`
(`disposed`, `already-disposed`, `planned`, `refused`), `mode`, `branch_kept`,
and `refusal` (`{code, detail}`, or `null`). Human output prints one line per
task and exits 2 when any task was refused.


## Recovering an interrupted collection

Collection runs inside the batch's own task lock. A crash between the worker
exiting and that collection leaves a durable receipt and no artifact:

```bash
brigade runs tasks collect <run> <task_id>
```

It never launches anything, and it holds the same two locks. The task must have
a recorded attempt (`not-attempted`) and a worktree binding, and the binding is
revalidated exactly as disposal validates it. Then:

- **No observation yet.** The durable receipt (`batch-result-*.json`) is the
  execution fact. Collection runs with the same replay rules as the batch, and
  the observation is appended exactly as the batch would have written it:
  `completed` or `failed` from the receipt, `artifact_kind` `commit` with the
  artifact sidecar digest when a candidate exists, otherwise `receipt` with the
  receipt digest. Provider correlation references cannot be recovered from a
  receipt and stay empty.
- **A receipt-only observation.** Collection runs, and a closed
  `task.artifact-collected` fact records the sidecar digest, the candidate and
  the delivery. The existing observation is never rewritten. Integration and
  disposal accept a commit artifact from the observation or from this fact, and
  refuse with `artifact-mismatch` if the two ever disagree.
- **A commit artifact already recorded.** The sidecar must still exist and hash
  to the recorded digest, or the command refuses with `artifact-mismatch`.
  Otherwise it reports `already-collected` and writes nothing.
- **A no-changes delivery already collected.** Its observation names the worker
  receipt, so the matching sidecar is the record of the batch's own collection.
  The command reports `already-collected` and writes nothing.
- **A failed collection.** A `collection-failed` delivery is not an artifact:
  nothing is recorded, and the command refuses with `collection-failed` and the
  closed reason, so it can be retried unchanged.

| Code | Refusal |
| --- | --- |
| `no-task-journal`, `journal-unreadable`, `unknown-run`, `duplicate-task` | As for dispose |
| `unknown-task` | The named task is not accepted by this run |
| `target-busy`, `task-busy` | Another caller holds the workspace or task run lock |
| `not-attempted` | No attempt was ever prepared, so there is nothing to collect |
| `workspace-invalid` | No binding, the binding does not match, or the checkout is gone |
| `no-receipt` | No durable worker receipt to build the observation from |
| `artifact-mismatch` | A sidecar is missing, altered, or disagrees with its digest |
| `collection-failed` | Collection itself failed; nothing was recorded and it can be retried |

JSON output uses `brigade.runs_tasks_collect.v1`, with `run_id`, `task_id`,
`status` (`collected`, `already-collected`), `delivery`, `candidate_commit`,
`artifact_digest`, and `refusal` (`null` on success).


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
`tasks` object keyed by task ID. Each member has `status`, `not_run`, `result_id`,
`result_ref` (relative to the run directory, or null), `workspace`
(`worktree` for a task that ran in its own reserved checkout, `shared` for one
that ran in the caller workspace), `artifact` (`{delivery, candidate_commit,
patch_ref, patch_sha256}` for a worktree task, null for a shared one), and
`refusal` (`{code, reason}` for a task
refused at its launch point, otherwise null). Human mode also prints each
refusal to stderr as `task <id>: <code> (<reason>)`. Completed results persist
as each worker finishes, as the same sanitized projection the run receipt uses
(no raw stdout or stderr captures). Failed workers retain their result receipt too. Skipped
tasks remain queued in the journal, have `not_run: true`, and have no result reference. Cancellation
and timeout preserve completed results and report unresolved tasks as stored.
Per-task cancellation is described in [Cancellation](#cancellation).
Exit 0 requires every task to be completed successfully and the run to pass
finalization. Worker failures, cancellation, and timeout exit 1. Preflight and
run-guard refusals retain their nonzero dispatch exit code and stderr reason.
Batch runs retain normal drift checkpoints, worker-result artifacts, final
receipts, artifact collection, and lineage metadata without calling synthesis.
Synthesis execution exits 2 with `synthesis-not-implemented`. Dry run still shows
its requested seat. No synthesis model call or budget is created.

## Cancellation

`brigade runs tasks cancel <run> <task_id>... [--reason operator|parent|budget] [--json]`
requests cancellation of the selected assignments. Each request targets one
task. `--reason` accepts the tokens shown above.

The journal records intent as `task.cancel-requested` and the worker outcome
separately as `task.observed`. The inbox reserves the `ack` kind and refuses it
with `ack-unsupported`.

A task journal may only be written by the active run owner. A cancel request
often comes from another process, so it never journals directly. It is first
written as one closed `brigade.task_inbox_request.v1` document under
`<run_dir>/task-inbox/`: kind, task id, reason token and a UTC timestamp, with no
paths and no free text. Requests are created with an exclusive no-follow create
and are never overwritten, so the first request for a task wins and a repeated
request is a no-op. They are also never deleted, because the file is the replay
source.

The owner drains the inbox before each launch claim and every 0.5 seconds
while dispatch is active. A journaled request for an unclaimed task blocks the
launch. Brigade records one `cancelled` observation with containment
`not-launched` on the task's deterministic attempt. Replay and resume drain
the inbox before preparing another dispatch.

For a claimed task, Brigade decides between completion and cancellation at
the worker result handoff. If the result reaches the batch before the watcher
claims cancellation, Brigade keeps the worker's status and result. A journaled
intent then appears as `cancel.too_late: true` in `runs tasks show` and
`runs tasks list`. If the watcher wins, Brigade terminates subprocesses
registered to that task and attempt, or requests interruption of that exact
app-server turn. On Windows it uses each subprocess's existing Job Object to
terminate the owned process tree. A worker or turn registering after the
watcher wins is also cancelled.

Once the worker returns, Brigade collects partial worktree changes through
the usual artifact collection path and records one `cancelled` observation.
When the stop was not confirmed (`still_active` or `error`), the worker may
still be writing its checkout, so Brigade commits nothing from it: the
observation rests on the receipt and the worktree is left as it is.
Containment reports `interrupted`, `still_active`, or `error` based on the
observed stop outcome, or `unknown` when no stop outcome was available. An
app-server interrupt acknowledgement alone does not prove that the turn
stopped. The batch joins the watcher when dispatch ends. A failed watcher poll
is reported and the next poll continues. A watcher that ends for any reason
stops advertising live termination, so `runs tasks cancel` never reports
`requested` without a watcher to act on it.

Fleet remote dispatch routes each task by seat, and it cannot cancel one
exact remote request. When a batch has a remote router, Brigade records where
each task went. The watcher stops a task only once the router has kept it on
this machine. A task routed to another machine, or not routed yet, is never
stopped and never recorded as `cancelled` after launch: it keeps its real
result, and the intent shows as `cancel.too_late: true`. The cancel command
promises `requested` only for a task known to run here and reports
`live-termination-unavailable` for the rest. Intent still keeps a task that
has not launched from launching, wherever it would have run.

Cancellation leaves same-seat siblings eligible to finish. It also counts as
a decided outcome for fail-fast, so unrelated tasks in later waves still run.
Only a cancellation the batch's own watcher performed counts. A worker that
reports `cancelled` on its own, with no cancel request, is a failure and still
stops later waves.
A task that `depends_on` a cancelled task never launches. The batch reports
it as `prerequisite-cancelled` and leaves it `queued` in the journal.

An unknown task reports `unknown-task`, and a task that is already terminal
when the command reads it reports `already-terminal`. Neither writes a request.

The inbox is bounded: at most 1024 request files of at most 4 KiB each. Exceeding
either bound refuses with `inbox-overflow`. Malformed request files are skipped
and reported by name. A symlinked inbox directory or request file is refused with
`inbox-refused`, and so is an entry swapped for a non-regular file after it was
inspected. The batch treats any inbox refusal as a warning and keeps launching,
so a damaged inbox cannot stop a batch. The `cancel` command tries to acquire
the workspace run lock without waiting. If it acquires the lock, it journals
the intent and reports `recorded`. An inbox or journal failure under that lock
is a refusal with exit 2.

JSON output uses `brigade.runs_tasks_cancel.v1`, with `run_id`, `reason`, a
`tasks` list of `{task_id, outcome, reason, live_termination}`, and `refusal`.
Closed outcomes are `recorded`, `queued`, `already-terminal`, `unknown-task` and
`not-recorded`. A `queued` request with `live_termination: "requested"` is
available for the live watcher to act on. `live-termination-unavailable`
means there is no live batch watcher to stop a potentially running task.
`null` means live termination did not apply. When writing one request fails,
the requests already written stay
in the answer, the failed task and any after it report `not-recorded`, and
`refusal` names the failure.
Exit 2 means at least one task was not recorded.

See [kinds of delegated work](execution-model.md#kinds-of-delegated-work) for how
batch tasks compare with ledger tasks, run-lineage children, remote fleet
requests, and T3 app-owned child tasks.
