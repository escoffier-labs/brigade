# Durable assignment journal

The programmatic API in `brigade.run_tasks` accepts one task at a time and
records assignment identity, observations, terminal result references, and
consumption. This is a partial foundation for #2049. It performs no dispatch,
model execution, notification delivery, cancellation, retry, or integration.

## API

```python
from brigade.run_tasks import (
    TaskSpec, TaskObservation, accept_task, prepare_attempt,
    observe_task, read_tasks, acknowledge_result,
)

handle = accept_task(
    run_dir, workspace=workspace, request_id="request-1",
    spec=TaskSpec(task_id="task-1", seat="worker", payload_digest=payload_sha256),
)
handle = prepare_attempt(run_dir, workspace=workspace, task_id="task-1")
result = observe_task(
    run_dir, workspace=workspace, handle=handle,
    observation=TaskObservation(
        status="completed", provider_task_id="provider-task-1",
        original_run_id="original-run-1", provider_result_id="provider-result-1",
        artifact_kind="patch", artifact_digest=patch_sha256,
        containment="quiescent",
    ),
)
results = read_tasks(run_dir)
consumed = acknowledge_result(
    run_dir, workspace=workspace, task_id="task-1", result_id=result.result_id,
)
```

Call mutations while holding `runguard.run_lock(workspace, run_dir=run_dir)`.
The run must already have an enrolled lifecycle journal and a valid recovery
checkpoint. Readers require no active owner. Every operation verifies the
bounded journal and checkpoint coverage and synchronizes its exact head before
returning durable task facts. An I/O refusal raises `TaskJournalError` with a
bounded diagnostic. A refused operation may already have committed; reread after
a successful durability barrier before making any further decision.

All four data types are frozen:

| Type | Fields |
| --- | --- |
| `TaskSpec` | `task_id`, `seat`, `payload_digest`; computed `spec_digest` |
| `TaskHandle` | `run_id`, `request_id`, `spec`, optional `attempt_id` |
| `TaskObservation` | `status`; optional `provider_task_id`, `original_run_id`, `provider_result_id`, `artifact_kind`, `artifact_digest`; `containment="unknown"`, `wait_timed_out=False` |
| `TaskResult` | `handle`, `status`; optional provider identities, `result_id`, artifact classification/digest, `version`; `containment="unknown"`, `acknowledged=False` |

`accept_task(...) -> TaskHandle` returns the existing handle for the same
request ID and specification, including an already prepared attempt. A reused
request ID with different input or a reused task ID with a different request
refuses. Two task IDs with the same seat and payload digest remain distinct.
`prepare_attempt(...) -> TaskHandle` creates the first stable attempt once.
It returns that attempt after restart, uncertainty, or a terminal result.
It cannot create a successor.

`observe_task(...) -> TaskResult` requires the exact accepted task and prepared
attempt. Once recorded, each provider correlation field is immutable. A later
backing-thread run cannot replace `original_run_id` or `provider_result_id`.
`read_tasks(...) -> tuple[TaskResult, ...]` preserves acceptance order and keeps
acknowledged results readable. `acknowledge_result(...) -> TaskResult` requires
the exact terminal result ID and deduplicates repeated consumption. Acknowledgement
records result consumption only. Neither completion nor acknowledgement releases
capacity or establishes descendant containment.

## State and result rules

Statuses are `queued`, `running`, `waiting-for-input`, `waiting-for-children`,
`unknown`, `completed`, `failed`, `interrupted`, and `cancelled`. The last four
are terminal. Initial acceptance is queued. Preparing an attempt leaves the
status queued because preparation supplies no evidence of execution.

A completed observation with active children becomes `waiting-for-children`
until the task has a terminal result. `wait_timed_out=True` leaves the task
unchanged and writes no execution fact. Cancellation intent and acknowledgement
are outside this slice. A cancelled status describes a caller-supplied terminal
observation; it proves no containment.

Containment is a separate observation: `unknown`, `active`, or `quiescent`.
Later active descendants can update containment while preserving an existing
terminal result. Callers must establish containment through a supported surface;
this journal does not inspect provider work. A completed observation with unknown
containment can publish an acknowledgeable result. Consumers must check containment
separately before releasing capacity.

Terminal result identity binds run, task, attempt, immutable provider
correlation, terminal status, and classified artifact digest. Supported artifact
kinds are `commit`, `patch`, `receipt`, and `unavailable`. A known artifact needs
a SHA-256 digest; unavailable bytes have no digest. Completion without supplied
artifact bytes is recorded as unavailable. A model summary supplies no artifact
bytes. This API records caller classifications and does not validate or retrieve
artifacts. Contradictory terminal replacement refuses.

An observation supplying any artifact classification or digest with active
containment refuses before journal mutation for every status, including terminal
statuses and an identical artifact resubmission. The caller must retain that
evidence and wait for suitable containment before publishing a terminal result
with the artifact. Publishing a terminal result with `unavailable` fixes its
identity: a later artifact cannot be attached. With active children and no
artifact, `failed`, `interrupted`, and `cancelled` publish terminal results
immediately, while `completed` records `waiting-for-children`. Callers that need
the artifact in the result must retain it and defer terminal publication as
appropriate. No pending artifact store is supplied.

Repeated identical current observations and acknowledgements add no records.
A state that recurs after a real transition records a new fact. The existing
journal byte/event bounds still apply, including to repeated real transitions.

`TaskResult.version` is the digest of its latest task fact, including preparation
and acknowledgement. It is reconstructed from the journal after restart and does
not change for identical no-op observations. Pass `expected_version=result.version`
to `observe_task` to compare and append under the same journal transaction. A
mismatch raises `TaskJournalError` before writing, including for an identical
observation. After an indeterminate write, reread and reconcile with the transport
observation before deciding whether to submit another observation.

`expected_version` is optional for compatibility with ordered callers. Callers
that retry or receive out-of-order transport observations must supply it and retain
the version against which the observation was collected. Refreshing the version
and blindly replaying stale input defeats the protection. Omitting it leaves
ordering to the caller and cannot distinguish a delayed retry from recurrence.
A true recurrence can be submitted with the current version after reevaluation.
No transport polling or observation ordering is inferred by this API.

## Persistence and crash boundaries

Task facts use the existing append-only `events/lifecycle.jsonl` journal:
`task.accepted`, `task.attempt-prepared`, `task.observed`, and
`task.acknowledged`. Each fact is paired with a recovery checkpoint. Its pairing
key binds the exact event request, and the aggregate run status is preserved
while the projector cursor advances. No second task database is created.

Task payloads have exact closed flat fields. Local task IDs use ASCII letters,
digits, and `._:-`, starting with a letter or
digit, with a 128-character limit matching live control. Request IDs and seats
retain the 512-character bound and the same character grammar. Provider correlation
references have a separate 512-character bound and admit ASCII letters, digits,
`._:%~+/@=-`, starting with a letter or digit. Percentencoded opaque handles such
as `node:delegated-task:command%3Afake-example` are retained without decoding.
Provider references are opaque identifiers only. Consumers must never interpret
them as paths or URLs, resolve them, or use them to carry secrets or provider
bodies, even when the character grammar admits path-shaped or URL-shaped values.
Whitespace, control characters and JSON bodies refuse. Digests are 64 lowercase hex
characters. Raw task text, prompts, provider bodies, summaries, credentials,
and paths have no task payload field. The input digest does not retain input
bytes. Future dispatch must require the separately retained immutable input
artifact and refuse when it is missing.

A partial tail, broken hash chain, unsupported reader, mismatched owner, or
failed durability barrier refuses. A checkpoint-only task tail raises an
`incomplete-pair` refusal for both reads and writes. Later unrelated lifecycle
facts cannot hide an earlier orphaned task pair. A reused old task checkpoint
cannot authorize an uncovered task append. Generic lifecycle checkpoint reuse
retains its existing behavior and recovery limitations. This slice deliberately
provides no automatic repair for that crash window. It preserves the evidence
and cannot infer acceptance or create another attempt. Exact manual
reconciliation or a separately reviewed recovery API is needed before progress.
A complete fact followed by fsync ambiguity can be read only after a fresh
successful exact-head barrier. Terminal results remain available after the run
owner exits. Further consumption acknowledgement still requires an active owner.

Generic lifecycle and dispatch writers hold their pair lock within one process,
but do not hold the cross-process journal lock across both pair members. A task
reader in another process can therefore transiently refuse with `incomplete-pair`
while a dispatch writer is between checkpoint and fact. It returns normally once
the writer completes the pair. The category alone does not distinguish a live
writer from a crash. Reread after the writer settles, and treat a persistent
refusal as reconciliation work. Task writers hold both locks across their pair.

Atomic batch acceptance, explicit retry decisions, transport identity propagation,
cancellation delivery, artifact retention/integration, and live T3 qualification
remain outside this slice. The journal supplies no Brigade/T3 budget integration.
