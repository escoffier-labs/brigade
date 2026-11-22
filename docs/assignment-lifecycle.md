# Live assignment control

This slice provides in-memory control handles. It does not accept or persist
assignments. The caller owns stable IDs and execution admission.

`ActiveTurn(worker, thread, thread_id, turn_id, task_id=None, attempt_id=None)`
preserves its original positional fields. `LiveTurnRegistry.register(worker,
thread, turn_id, *, task_id=None, attempt_id=None)` stores one current turn per
task/attempt pair. Different pairs sharing a worker coexist. Re-registering a
pair replaces only that pair. Registrations without IDs retain the legacy
single slot per worker. Task and legacy slots have separate namespaces.

`register` returns the frozen `ActiveTurn` record as a cleanup ownership handle.
Retain that exact object and pass it to `unregister(worker, turn_id, *,
task_id=None, attempt_id=None, registration=None)`. Cleanup with a handle removes
only that registration from the matching slot and turn. A stale handle cannot
remove its replacement, even if the thread, turn, worker, task, and attempt IDs
all match. Provider turn IDs are scoped to their thread, not globally unique.
Re-registering the same thread and turn still creates a new ownership handle.

Omitting the handle preserves the legacy slot/worker/turn matching behavior.
That form cannot distinguish replacements with reused IDs and should only be
used when the caller prevents overlapping registration and cleanup. Wrappers
around `register` must return its handle to preserve ownership protection.
Callers must retain the returned handle. If registration raises or no handle
is returned, there is no cleanup ownership and cleanup must be skipped.

IDs are nonempty ASCII tokens of at most 128 characters, beginning
with a letter or digit and otherwise using letters, digits, `.`, `_`, `:`, `-`.
An attempt requires a task ID.

`interrupt(worker=None, *, task_id=None, attempt_id=None)` and
`steer(worker=None, text="", *, task_id=None, attempt_id=None)` select explicitly.
Worker and task selectors are mutually exclusive. Task-only selection requires
one active attempt. Worker aliases require one matching turn. Ambiguity refuses
before calling a provider with code `ambiguous-target` and a sorted list of at
most 10 task/attempt IDs. Legacy entries appear as `<legacy>`. Unknown targets
use the existing `no-active-turn` code. A task ID is never inferred from a
worker string, even if both strings are identical.

Control socket requests use the same `task_id` and optional `attempt_id` fields:
`{"op":"interrupt","task_id":"task-a","attempt_id":"attempt-1"}`.
Steering also requires `text`. Existing worker requests and response fields
remain valid. Assignment-aware responses add task and attempt metadata. An
interrupt with no target requests interruption of every snapshot turn and
retains the existing fail-fast provider-error behavior.

Registry snapshots are taken under a lock. Provider calls occur outside it.
An interrupt response acknowledges a request only. It does not establish that
execution or owned children stopped, and it does not unregister a turn.
`interrupt_observed(workers)` attempts every matching snapshot turn, including
same-seat siblings after an error. It retains the existing per-seat result
shape, reports `error` if any sibling request fails, and reports every seat
still registered after the requests. Neither provider acknowledgement nor
missing registration proves containment.

The appserver runner accepts optional task/attempt keywords and forwards them
to turn registration and cleanup. Its `finally` cleanup retains and supplies
the registration handle for both assignment and legacy worker slots, on success
and provider failure. It derives cleanup identity from that handle and never
uses tokenless cleanup. A swallowed registration-observer error leaves any
replacement intact. If a later callback fails after an earlier registration
succeeded, cleanup uses the earlier handle and its original identity.
Ordinary dispatch supplies no assignment IDs and uses legacy worker slots.
An explicit batch assignment carries `task_id` and `attempt_id`, and the
transport passes both to the appserver runner, so a batch turn registers under
`("task", task_id, attempt_id)`. `brigade runs interrupt --task-id` then reaches
one of two same-seat tasks, and a worker-name request for that seat reports
`ambiguous-target` with the task IDs. Direct CLI workers have no per-task
control path yet: the process registry still cancels as a whole. Parent
delivery, T3 children, and cancellation containment remain integration work
under #2046/#2049.


## Reading task status and results

`brigade runs tasks list <run> [--json]` prints one row per accepted task: task
id, status, attempt id, whether the status is terminal, whether its result was
acknowledged, containment, result id, and artifact classification and digest.
`brigade runs tasks show <run> <task_id> [--json]` prints the same fields for
one task, the batch task's `selection` (`{"kind": "seat"|"capability",
"capability": tag|null}`) when its request marker is readable, and, when the
batch result receipt exists, its run-relative path and SHA-256 digest. Neither
command prints task text, prompt text, receipt bodies,
or worker output. An unknown task exits 2 with code `unknown-task` and at most
10 sorted known task ids. A missing task journal exits 2 with code
`no-task-journal`.

`brigade runs tasks wait <run> [<task_id>...] [--timeout SECONDS] [--json]`
polls the journal at a bounded interval until every named task (default all) is
terminal. It exits 0 when all are terminal and 3 on timeout with the
non-terminal ids listed. `waiting-for-children`, `waiting-for-input` and
`unknown` are not terminal. A timeout records nothing and never cancels or
changes execution. JSON output uses `brigade.runs_tasks.v1`.

The verbs append no journal facts and change no task state. Each read uses the
same verified reader as the `brigade t3` verbs: it holds the journal lock for
the length of one read and confirms the journal head is durable, so it never
folds a half-written fact. A run directory on a read-only filesystem therefore
cannot be read yet. `wait` with no `--timeout` reports `task-journal-unreadable`
after 20 consecutive refused reads, and refuses with `no-tasks` when the run has
no accepted tasks. The result receipt is hashed only when it is a regular file
under 64 MiB, opened without following a symlink.


## Durable task control requests

Use `brigade runs steer <run> --task-id <task> [--attempt-id <attempt>] <text>`
or `brigade runs interrupt <run> --task-id <task> [--attempt-id <attempt>]`.
The positional worker form and `--worker <worker>` remain available. Worker
and task selectors are mutually exclusive. Interrupt without a selector targets
all active turns. The existing identifier bounds apply before journal mutation
or transport delivery.

Journal-backed requests record the requested task and optional attempt in their
fingerprint. Reusing a request ID with different selectors conflicts. Replaying
a recorded result restores those requested selectors without another send.
A task-only request does not acquire an attempt selector from the provider's
response. Its replay reports the requested task, without a resolved attempt.
Successful targeted responses must explicitly identify the selected task and,
when requested, the exact attempt. Missing or conflicting identity produces a
protocol failure rather than acknowledged delivery. Task-targeted requests use
distinct wire operations so older servers reject them before provider action.
Legacy runs forward selectors without durable replay, with the same transport
response identity checks.

Task redaction remaps control selectors and paired request identities alongside
task attempts and checkpoints. Aliases of recomputed spec, attempt, and result
identities are remapped in related task/provider and control references, even
outside the selected range. Selected control facts scrub private digests and
turn references while preserving closed operation and failure classifications.
For affected targeted controls, redaction refuses an invalid operation, unmatched
durable task, or unmatched durable attempt before publishing any changes. Live controls
may use external selectors, but redaction cannot safely remap those selectors
without their durable history. Refusal preserves the original recoverable bytes
and never substitutes a different attempt.
An interrupt observation acknowledges delivery. Task completion, execution
quiescence, and capacity release still require their own lifecycle evidence.


## Per-task worktrees

An explicit batch task may declare `isolation: "worktree"`. Before any worker
in that batch is submitted, and in plan order, execution reserves, creates,
validates and records one checkout for the task, then runs that worker in it.
Shared tasks, and everything the run itself writes, keep the caller workspace:
cassettes, receipts, the task journal, the run budget, retained-error notes and
caller run-lock resolution.

`task.workspace-bound` records the binding as SHA-256 digests only, at run event
schema version 3. A binding is custody of a checkout, not launch permission: the
task stays queued and attempt-free, so the relaunch fence treats it as never
attempted. A crash after the binding and before the attempt replays: the path is
recomputed, the digests and checkout are re-validated, and the same checkout is
reused. A crash after the attempt is refused by the existing fence, and no
second checkout is prepared.

Immediately before each launch the checkout is validated again with a pristine
status, so a file added after creation refuses that task. A workspace refusal
never deletes anything and refuses only its own task. A batch whose caller
workspace no longer matches the pinned base is refused whole with
`source-changed`, and one that would route through Fleet Hub is refused with
`isolation-remote-unsupported`, both before any launch. Each task checkout holds
its own run lock from creation through worker exit, so `brigade runs
prune-worktrees` skips it. Task checkouts and their branches are kept after the
run on every outcome.

This separates cooperative workers. It is not a security boundary: a worker with
full filesystem access can still reach the caller checkout by absolute path, and
every checkout shares the repository's Git common directory. Only a `codex` seat
on the `workspace-write` sandbox is confined to its folder. Uncommitted parent
edits are never copied; a task starts at the pinned commit.
