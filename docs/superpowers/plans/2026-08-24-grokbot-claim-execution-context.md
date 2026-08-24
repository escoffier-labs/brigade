# Grok Bot claim execution context implementation plan

**Goal:** Let an authenticated role-scoped Grok Bot worker receive its complete bounded task only after a successful claim, while every CLI, list, status, and operator surface stays redacted.

**Architecture:** Keep `grokbot_jobs.claim()` as the safe projection API. Add `claim_execution_context()` over one shared locked claim helper, then use it only from the worker MCP claim branch. The context is an exact copy of validated envelope fields. Tool inventories, storage schema, routes, credentials, and dependencies do not change.

**Key technology:** Python, pytest, file-locked JSON queue state, Streamable HTTP MCP.

Execute the tasks in order. Keep the checkboxes current. Every behavior change starts with a failing test run captured through Brigade.

## File map

- `src/brigade/grokbot_jobs.py`: atomic claim transition and exact execution-context projection.
- `tests/test_grokbot_jobs.py`: internal context, idempotency, conflict, expiry, and CLI-redaction contract.
- `src/brigade/grokbot_mcp.py`: worker-only claim handoff.
- `tests/test_grokbot_mcp.py`: role and disclosure boundary.
- `docs/grokbot-mcp.md`: listener and routine behavior.
- `docs/technical-guide.md`: safe projection exception for authenticated post-claim handoff.

## demi

Actual ask: allow scheduled Grok Bot workers to execute a queued task without an operator repeating its instructions in chat.

Smallest useful slice: add one whitelisted context member to the existing successful worker claim response.

Highest rung that holds: one local queue operation plus one existing adapter branch.

Existing pattern to follow: `_projection()` in `grokbot_jobs.py` and the role-filtered claim branch in `grokbot_mcp.py`.

Cut from scope: new tools, schema versions, secret scanners, task artifact services, routine UI, and changes to list or status.

Growth trigger: add a separate context retrieval tool only if workers need to re-fetch context after leaving the claimed state.

Verification: focused queue and adapter tests, MCP-extra tests, full `scripts/verify`, edge inventories, and live no-prompt canaries.

### Task 1: Add the atomic claim-context queue operation

**Files:**

- Modify: `tests/test_grokbot_jobs.py`
- Modify: `src/brigade/grokbot_jobs.py`

- [x] Add a failing test for the exact context and safe claim separation:

```python
def test_claim_execution_context_is_exact_and_safe_claim_stays_redacted(tmp_path: Path):
    context_job = _enqueue(tmp_path, idempotency_key="request-context")
    claimed = grokbot_jobs.claim_execution_context(
        tmp_path, context_job, "bot-a", "lease-a", 60, now=NOW
    )

    assert claimed["execution_context"] == {
        "label": "Add queue foundation",
        "role": "implementation-worker",
        "repository": "example/brigade",
        "base_ref": "main",
        "ownership_paths": ["src/brigade/grokbot_jobs.py", "tests/test_grokbot_jobs.py"],
        "instructions": "Build the private queue module.",
        "verification_commands": ["pytest -q tests/test_grokbot_jobs.py"],
        "artifact": {"kind": "draft-pr"},
        "timeout_seconds": 900,
    }
    assert grokbot_jobs.claim_execution_context(
        tmp_path, context_job, "bot-a", "lease-a", 60, now=NOW
    ) == claimed

    safe_job = _enqueue(tmp_path, idempotency_key="request-safe")
    safe = grokbot_jobs.claim(tmp_path, safe_job, "bot-a", "lease-safe", 60, now=NOW)
    assert "execution_context" not in safe
```

- [x] Add a failing disclosure test:

```python
def test_claim_execution_context_rejects_conflicting_and_expired_leases(tmp_path: Path):
    job_id = _enqueue(tmp_path, idempotency_key="request-context-boundary")
    grokbot_jobs.claim_execution_context(tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW)

    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^lease-conflict$"):
        grokbot_jobs.claim_execution_context(tmp_path, job_id, "bot-a", "lease-b", 60, now=NOW)
    with pytest.raises(grokbot_jobs.GrokbotJobError, match="^lease-expired$"):
        grokbot_jobs.claim_execution_context(
            tmp_path, job_id, "bot-a", "lease-a", 60, now=NOW + timedelta(seconds=60)
        )
```

- [x] Run RED through Brigade:

```bash
brigade work verify run --target . --command ".venv/bin/pytest -q tests/test_grokbot_jobs.py -k claim_execution_context" --capture brigade-work
```

Expect collection or execution to fail because `claim_execution_context` does not exist.

- [x] Add `claim_execution_context()` and route both public operations through one `_claim_job()` helper. The helper validates inputs once, holds `_queue_lock()`, performs the existing transition unchanged, and formats the result before leaving the lock:

```python
def claim_execution_context(
    target: Path,
    job_id: str,
    bot_id: str,
    lease_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Claim a queued job and return its validated worker context."""
    return _claim_job(target, job_id, bot_id, lease_id, lease_seconds, now, include_context=True)


def _claim_result(record: dict[str, Any], *, include_context: bool) -> dict[str, Any]:
    result = _projection(record)
    if include_context:
        result["execution_context"] = _execution_context(record)
    return result


def _execution_context(record: dict[str, Any]) -> dict[str, Any]:
    spec = record["spec"]
    assert isinstance(spec, dict)
    return {
        "label": spec["label"],
        "role": spec["role"],
        "repository": spec["repository"],
        "base_ref": spec["base_ref"],
        "ownership_paths": list(spec["ownership_paths"]),
        "instructions": spec["instructions"],
        "verification_commands": list(spec["verification_commands"]),
        "artifact": dict(spec["artifact"]),
        "timeout_seconds": record["timeout_seconds"],
    }
```

`claim()` calls `_claim_job(..., include_context=False)`. Same-lease idempotency uses `_claim_result()` on the existing record. Do not change state rules, timestamps, lease bounds, or `_projection()`.

- [x] Run GREEN with the Task 1 focused command. Expect the new tests and existing claim tests to pass.

- [x] Strengthen the existing CLI lifecycle assertion with:

```python
assert "execution_context" not in claimed
```

- [x] Commit:

```bash
git add src/brigade/grokbot_jobs.py tests/test_grokbot_jobs.py
git commit -m "feat: return bounded Grok Bot claim context"
```

### Task 2: Hand context only to the role-matching MCP worker

**Files:**

- Modify: `tests/test_grokbot_mcp.py`
- Modify: `src/brigade/grokbot_mcp.py`

- [x] Extend `test_workers_only_list_claim_and_read_their_configured_role` with the failing exact-context assertion:

```python
assert claimed["execution_context"] == _spec("implementation-worker")
assert "PRIVATE_INSTRUCTIONS_MUST_NOT_LEAK" not in json.dumps(listed)
assert "execution_context" not in worker.call_tool("grokbot_queue_status", {"job_id": implementation})
```

- [x] After the existing cross-role rejection, assert the scout job is still queued:

```python
assert grokbot_jobs.get_job(tmp_path, scout)["state"] == "queued"
```

- [x] Run RED through Brigade:

```bash
brigade work verify run --target . --command ".venv/bin/pytest -q tests/test_grokbot_mcp.py -k workers_only_list_claim" --capture brigade-work
```

Expect failure because the worker claim has no `execution_context`.

- [x] Change only the worker claim call in `GrokbotAdapter._call()`:

```python
return grokbot_jobs.claim_execution_context(
    self.config.target,
    job["job_id"],
    self.config.bot_id,
    _lease_id(arguments),
    LEASE_SECONDS,
)
```

- [x] Run GREEN with the Task 2 focused command.

- [x] Run the complete focused contract with the MCP extra present:

```bash
brigade work verify run --target . --command ".venv/bin/pytest -q tests/test_grokbot_jobs.py tests/test_grokbot_mcp.py" --capture brigade-work
```

Expect no skips from missing `mcp.server` and every test to pass.

- [x] Commit:

```bash
git add src/brigade/grokbot_mcp.py tests/test_grokbot_mcp.py
git commit -m "feat: hand claimed context to Grok Bot workers"
```

### Task 3: Document, verify, and publish the contract

**Files:**

- Modify: `docs/grokbot-mcp.md`
- Modify: `docs/technical-guide.md`
- Modify: `docs/superpowers/plans/2026-08-24-grokbot-claim-execution-context.md`

- [ ] In `docs/grokbot-mcp.md`, state that a successful worker claim returns the bounded validated envelope, while list, status, operator, and CLI surfaces stay redacted. Add the routine sequence: list, claim, validate role and repository, start, renew before expiry, complete or fail.

- [ ] In `docs/technical-guide.md`, replace the blanket claim that all mutation commands are safe projections with the exact exception for authenticated worker MCP claim. Keep the CLI statement unchanged.

- [ ] Run Vale through Brigade:

```bash
brigade work verify run --target . --command "vale docs/grokbot-mcp.md docs/technical-guide.md docs/superpowers/specs/2026-08-24-grokbot-claim-execution-context-design.md docs/superpowers/plans/2026-08-24-grokbot-claim-execution-context.md" --capture brigade-work
```

- [ ] Run the full repository gate:

```bash
brigade work verify run --target . --command "scripts/verify" --capture brigade-work
```

Expect lint, format, typing, snapshots, all tests, and the coverage threshold to pass.

- [ ] Run `git diff --check` and the public-content guard. Confirm no private hostnames, addresses, account data, credentials, absolute home paths, or model-authorship prose appears in the branch diff.

- [ ] Tick every completed checkbox and commit the docs plus plan:

```bash
git add docs/grokbot-mcp.md docs/technical-guide.md docs/superpowers/plans/2026-08-24-grokbot-claim-execution-context.md
git commit -m "docs: explain Grok Bot claim handoff"
```

- [ ] Open a non-draft PR with `Fixes #1167`. Merge only after CI and one independent security review pass.

## Deployment acceptance

- [ ] Install the merged commit beside the existing Brigade CLI, regenerate the three native units, and restart one role at a time with rollback to the retained legacy unit on failure.
- [ ] Run local canary plus Access-only 401, origin-only 401, and exact-inventory edge checks for all roles.
- [ ] Enqueue one Repository Scout job whose label does not contain its instructions. Send the Bot only the job ID and tell it to follow the returned claim capsule. Require claim, start, renew, report, and complete.
- [ ] Enqueue one bounded Implementation Worker job. Send only the job ID. Require a draft PR, verification evidence, and no merge or deployment.
- [ ] Reconcile both artifacts through Brigade cloud tracking.
- [ ] Save each proven workflow as a Grok Bot skill. Create staggered routines with one role-matching job maximum per run, no merge or deploy, and explicit no-data and failure behavior.
