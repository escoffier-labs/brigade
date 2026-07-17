# Environment override log scrubbing implementation plan

### Task 1: Scrub resolved values at direct-agent output

**Files:** `src/brigade/agents.py`, `tests/test_agents.py`

- [x] Add failing tests for successful stdout, failed stderr and detail, overlapping override values, and unrelated parent environment values.
- [x] Run the focused agent tests through Brigade and confirm RED.
- [x] Add exact nonempty replacement after process execution and structured-output parsing, before any `AgentResult` return.
- [x] Run the same Brigade command to GREEN.
- [x] Commit the direct-agent scrubber.

### Task 2: Prove persisted worker logs are safe

**Files:** `tests/test_run_transport_env.py`

- [x] Add a failing dispatch and persistence test whose process stdout and stderr echo a resolved reference value.
- [x] Assert the worker text, detail, stdout, stderr, and stored log files contain `[OVERRIDE_NAME]` and not the value.
- [x] Run the focused transport test through Brigade to GREEN.
- [x] Commit the persistence coverage.

### Task 3: Revalidate environment tables on resume

**Files:** `src/brigade/run_resume.py`, `tests/test_run_resume.py`

- [x] Add failing snapshot tests for an inline secret and a colliding target.
- [x] Run the focused resume tests through Brigade and confirm RED.
- [x] Reuse roster `_as_env` while rebuilding snapshot agents and report invalid snapshots without dispatch.
- [x] Run the same Brigade command to GREEN.
- [x] Commit resume validation.

### Task 4: Document and publish

**Files:** `docs/technical-guide.md`, all changed files

- [x] Document the exact-value scrub boundary and snapshot revalidation.
- [x] Run all focused coverage and the full pytest suite through Brigade.
- [x] Review the exact diff against #310 and request independent review.
- [x] Verify each sendback claim before changing code, then repeat affected gates.
- [ ] Publish an exact-tree PR closing #310, monitor CI and review threads, and merge only at a clean expected head.
- [ ] Run the merged-head gate and write a linted memory handoff.
