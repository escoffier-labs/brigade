# Bounded direct-Grok recovery implementation plan

**Goal:** Land issue #282 with one typed exact-session continuation, one explicit Cursor-Grok ACP fallback, and complete attempt receipts for direct Grok read-only workers.

**Architecture:** `agents.py` owns Grok envelope parsing and exact-session CLI construction. `roster.py` owns the explicit fallback reference and validates its transport. `run_transport.py` owns the bounded state machine because it already has the selected seat and all dispatch settings. `run_receipts.py` serializes and logs every attempt.

**Execution:** Complete each task in order, keep the checkboxes current, run every test through `brigade work verify run`, and commit each green slice.

## File map

- `src/brigade/agents.py`: extract Grok session metadata and build one exact-session continuation.
- `src/brigade/roster.py`: parse and validate `invalid_final_fallback`.
- `src/brigade/run_transport.py`: represent attempts and execute the bounded recovery policy.
- `src/brigade/run_receipts.py`: serialize attempt metadata and persist per-attempt logs.
- `tests/test_agents.py`: adapter metadata and continuation argv coverage.
- `tests/test_roster.py`: fallback-reference schema coverage.
- `tests/test_run_transport_recovery.py`: typed trigger and bounded transport-policy coverage.
- `tests/test_aboyeur.py`: direct-worker artifact and terminal-result coverage.
- `docs/seat-catalog.md`: public roster syntax and constraints.

### Task 1: Exact Grok continuation support

**Files:** `tests/test_agents.py`, `src/brigade/agents.py`

- [x] Add failing adapter tests asserting that valid and malformed Grok JSON envelopes populate `session_id`, `request_id`, and `stop_reason`, and that `resume_session_id` produces one argv containing `--resume <id>`, the original model/reasoning/sandbox, and the JSON schema.
- [x] Run RED:
  `brigade work verify run --target . --command ".venv/bin/python -m pytest tests/test_agents.py -q" --capture brigade-work`
  Expect failures because `run_agent` has no continuation argument and drops envelope metadata.
- [x] Replace the parser tuple with a frozen result object and thread its metadata into every structured-Grok `AgentResult`:

```python
@dataclass(frozen=True)
class _GrokFinal:
    text: str
    error: str
    session_id: str | None
    request_id: str | None
    stop_reason: str | None
```

- [x] Add `resume_session_id: str | None = None` to `build_argv` and `run_agent`. Reject it for non-Grok and writable calls. For Grok, insert `--resume`, the exact ID, and the continuation prompt before the generated read-only flags while retaining model, reasoning, sandbox, and schema arguments.
- [x] Run the same Brigade command to GREEN.
- [x] Commit: `feat(run): support exact Grok continuation`

### Task 2: Explicit fallback roster contract

**Files:** `tests/test_roster.py`, `src/brigade/roster.py`, `docs/seat-catalog.md`

- [x] Add failing roster tests for one valid reference plus missing seat, non-Grok source, direct Cursor target, non-Grok Cursor model, and wrong ACPX version.
- [x] Run RED:
  `brigade work verify run --target . --command ".venv/bin/python -m pytest tests/test_roster.py -q" --capture brigade-work`
  Expect the new field to be ignored or absent.
- [x] Add the field and parse it as a non-empty seat name:

```python
@dataclass(frozen=True)
class Agent:
    # existing fields
    invalid_final_fallback: str | None = None
```

- [x] After all agents are parsed, validate that a configured source is direct Grok and its target exists, is not the orchestrator, uses `cli = "cursor"`, has a `grok-*` model, and uses reviewed ACPX transport/version. Do not require the field when no recovery is needed.
- [x] Document the TOML field and its fail-closed constraints in `docs/seat-catalog.md`.
- [x] Run the same Brigade command to GREEN.
- [x] Commit: `feat(roster): validate Grok fallback seats`

### Task 3: Attempt receipt model and logs

**Files:** `tests/test_run_transport_recovery.py`, `src/brigade/run_transport.py`, `src/brigade/run_receipts.py`

- [x] Add failing serialization tests for required attempt fields, null exit codes, selected flags, and distinct stdout/stderr log references.
- [x] Run RED:
  `brigade work verify run --target . --command ".venv/bin/python -m pytest tests/test_run_transport_recovery.py -q" --capture brigade-work`
  Expect import or field failures because attempts are not modeled.
- [x] Add a frozen attempt record and attach it to `WorkerResult`:

```python
@dataclass(frozen=True)
class WorkerAttempt:
    kind: str
    worker: str
    task: str
    transport: str
    model: str | None
    reasoning: str | None
    started_at: str
    finished_at: str
    exit_code: int | None
    terminal_reason: str
    failure_phase: str | None
    failure_kind: str | None
    session_id: str | None
    selected: bool = False
    stdout: str | None = None
    stderr: str | None = None
    stdout_log: str | None = None
    stderr_log: str | None = None
```

- [x] Serialize `attempts` only when present, always emitting the required machine fields. Extend `write_worker_logs` to write `worker-NNN-<seat>-attempt-NNN-<kind>.stdout.log` and `.stderr.log`, then store their relative paths on the attempt.
- [x] Run the same Brigade command to GREEN.
- [x] Commit: `feat(run): persist worker attempt receipts`

### Task 4: Bounded recovery state machine

**Files:** `tests/test_run_transport_recovery.py`, `src/brigade/run_transport.py`

- [x] Add failing policy tests for first-attempt success, continuation recovery, fallback recovery, missing fallback, all attempts invalid, missing session ID, nonzero initial failure, continuation timeout, writable dispatch, and orchestrated dispatch.
- [x] Run RED:
  `brigade work verify run --target . --command ".venv/bin/python -m pytest tests/test_run_transport_recovery.py -q" --capture brigade-work`
  Expect only the first dispatch call and no attempt ledger.
- [x] Define the sole trigger and fixed continuation request:

```python
_GROK_CONTINUATION_PROMPT = (
    "Return the final answer now using the required structured answer schema. "
    "Do not narrate progress or repeat the task."
)

def _is_direct_grok_invalid_final(agent: Agent, result: agents.AgentResult, *, direct: bool, read_only: bool) -> bool:
    return (
        direct
        and read_only
        and agent.cli == "grok"
        and agent.transport == "direct"
        and result.failure_phase == "output-validation"
        and result.failure_kind == "malformed-final-output"
    )
```

- [x] Refactor the existing transport branches into one inner invocation helper without changing legacy call shapes. Record the initial attempt. On the exact trigger, require `session_id`, run one continuation with identical settings, and only on the same typed continuation failure run the explicitly named ACP target once with the original task. Never call the recovery helper for the fallback result.
- [x] Mark exactly one successful attempt selected. When no fallback is configured after an invalid continuation, return `failure_kind = "grok-fallback-missing"`; when the first envelope lacks a session ID, return `failure_kind = "grok-session-missing"`.
- [x] Run the same Brigade command to GREEN.
- [x] Commit: `feat(run): add bounded Grok recovery`

### Task 5: Direct-worker artifact contract

**Files:** `tests/test_aboyeur.py`, `src/brigade/run_receipts.py`, `src/brigade/run_transport.py`

- [x] Add failing end-to-end direct-worker tests that invoke `aboyeur.run` and inspect `worker-results.json`, `synthesis.json`, `final.txt`, and every attempt log for first success, continuation success, fallback success, missing fallback, and all-invalid failure.
- [x] Run RED:
  `brigade work verify run --target . --command ".venv/bin/python -m pytest tests/test_aboyeur.py -q" --capture brigade-work`
  Expect artifact assertions to fail until the receipt and selected-result mapping are complete.
- [x] Make only the minimal receipt or result-mapping corrections exposed by those tests. Keep the existing top-level worker fields as the selected or terminal result so old readers remain compatible.
- [x] Run the same Brigade command to GREEN.
- [x] Commit: `test(run): cover bounded Grok recovery artifacts`

### Task 6: Full verification and publication

**Files:** all changed files

- [x] Run focused recovery coverage through Brigade:
  `brigade work verify run --target . --command ".venv/bin/python -m pytest tests/test_agents.py tests/test_roster.py tests/test_run_transport_recovery.py tests/test_aboyeur.py -q" --capture brigade-work`
- [x] Run the full repository gate through Brigade:
  `brigade work verify run --target . --command "./scripts/verify" --capture brigade-work`
- [x] Review the exact diff and verify issue #282 acceptance criteria against code and receipts.
- [x] Request independent review, verify every claim before changing code, and repeat the focused and full gates after accepted fixes.
- [ ] Push the branch, open a ready PR that closes #282, monitor checks and review threads, merge only when the head is clean and green, then write and lint the memory handoff.
