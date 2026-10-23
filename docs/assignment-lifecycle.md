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
Main dispatch and the transport runner
protocol currently supply no assignment IDs. Until the owning integration
adds those IDs, ordinary dispatch uses legacy worker slots. Journals, durable
results, acceptance deduplication, parent delivery, wait semantics, T3 children,
and cancellation containment remain integration work under #2046/#2049.
