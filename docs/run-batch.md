# Explicit batch dry run

`brigade run batch batch.json --dry-run [--json]` validates a caller-supplied
batch and prints deterministic topological layers, task seats, and a SHA-256
request fingerprint. It makes zero planner, worker, or synthesis calls and
requires no roster. Seat selection is syntactic only: execution-time roster
validation and admission are not implemented. Without `--dry-run`, a valid
batch is refused with exit 2: `batch execution not yet implemented (#2047)`.
Errors also exit 2, with a stable code in text or `error.code` in JSON.

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
Selecting a task by admitted capability instead of a named seat, which #2047
also allows, is not part of v1. It needs the admission wiring that execution
will add.

The batch admits 1 through 8 tasks, matching the existing durable assignment
batch bound. Each task or synthesis text is limited to 16,384 UTF-8 bytes.
The CLI reads at most 262,144 input bytes, bounding JSON parsing overhead.
Task IDs and request IDs contain 1 through 128 ASCII characters, starting
with a letter or digit, followed by letters, digits, `.`, `_`, `:`, or `-`.
Seat tokens use the same alphabet with a 512-character limit.

`task_id` is preserved verbatim as the existing `run_tasks.TaskSpec.task_id`
and `run_control.ActiveTurn.task_id`. It uses `run_task_journal.task_id` for
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
same request ID. This helper provides no storage or resume behavior.

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
| `execution-not-implemented` | Valid batch submitted without `--dry-run` |
