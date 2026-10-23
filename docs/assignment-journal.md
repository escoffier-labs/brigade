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
| `TaskResult` | `handle`, `status`; optional provider identities, `result_id`, artifact classification/digest; `containment="unknown"`, `acknowledged=False` |

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
the exact terminal result ID and deduplicates repeated consumption.

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
this journal does not inspect provider work.

Terminal result identity binds run, task, attempt, immutable provider
correlation, terminal status, and classified artifact digest. Supported artifact
kinds are `commit`, `patch`, `receipt`, and `unavailable`. A known artifact needs
a SHA-256 digest; unavailable bytes have no digest. Completion without supplied
artifact bytes is recorded as unavailable. A model summary supplies no artifact
bytes. This API records caller classifications and does not validate or retrieve
artifacts. Contradictory terminal replacement refuses.

Repeated identical current observations and acknowledgements add no records.
A state that recurs after a real transition records a new fact. The existing
journal byte/event bounds still apply, including to repeated real transitions.

## Persistence and crash boundaries

Task facts use the existing append-only `events/lifecycle.jsonl` journal:
`task.accepted`, `task.attempt-prepared`, `task.observed`, and
`task.acknowledged`. Each fact is paired with a recovery checkpoint. Its pairing
key binds the exact event request, and the aggregate run status is preserved
while the projector cursor advances. No second task database is created.

Task payloads have exact closed flat fields. Identifiers use ASCII letters,
digits, periods, underscores, colons, and hyphens, starting with a letter or
digit, within the existing 512-character bound. Digests are 64 lowercase hex
characters. Raw task text, prompts, provider bodies, summaries, credentials,
and paths have no task payload field. The input digest does not retain input
bytes. Future dispatch must require the separately retained immutable input
artifact and refuse when it is missing.

A partial tail, broken hash chain, unsupported reader, mismatched owner, or
failed durability barrier refuses. A checkpoint-only task tail raises an
`incomplete-pair` refusal for both reads and writes. This slice deliberately
provides no automatic repair for that crash window. It preserves the evidence
and cannot infer acceptance or create another attempt. Exact manual
reconciliation or a separately reviewed recovery API is needed before progress.
A complete fact followed by fsync ambiguity can be read only after a fresh
successful exact-head barrier. Terminal results remain available after the run
owner exits; further consumption acknowledgement still requires an active owner.

Atomic batch acceptance, explicit retry decisions, transport identity propagation,
cancellation delivery, artifact retention/integration, and live T3 qualification
remain outside this slice. The journal supplies no Brigade/T3 budget integration.
