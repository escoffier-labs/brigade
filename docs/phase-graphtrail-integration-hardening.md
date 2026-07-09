# GraphTrail Integration Hardening

Goal: stop recursive Brigade receipt growth and ensure GraphTrail pre-edit context is attached when an indexed target and task are available.

Architecture: verification receipts reference the previous receipt by stable metadata only, never by embedding its full payload. Work briefs reuse the existing GraphTrail context renderer and record a compact attachment result without turning missing GraphTrail into a hard failure.

## File map

- `src/brigade/work_cmd/verification.py`, `tests/test_work_cmd_verification.py`: previous-receipt evidence compaction.
- `src/brigade/work_cmd/session.py`, `src/brigade/context_cmd.py`, relevant tests: pre-edit GraphTrail brief attachment.
- `docs/` and changelog/release notes where the integration contract is documented.

### Task 1: Compact previous verification evidence

- [x] Add a failing test that creates a prior receipt containing nested `evidence`, runs a second verification, and asserts the new receipt contains only prior `run_id`, status, path, and digest, with no nested `evidence` key.
- [x] Run the focused test; expect failure because the full previous receipt is embedded.
- [x] Replace full-payload embedding with a compact reference derived from the prior receipt and its digest.
- [x] Run the focused test and receipt-signing tests; expect pass.
- [x] Commit `fix(work): reference prior verification receipts compactly`.

### Task 2: Attach GraphTrail context before work

- [x] Add a failing test for a target with `.graphtrail/graphtrail.db`, a fake GraphTrail binary, and a selected task. Assert `work brief --json` contains an attached code-graph brief and a nonzero context record.
- [x] Run the focused test; expect failure because current work briefs report no attached context.
- [x] Reuse the existing context helper from the work-brief path, keep missing binary/db/task fail-open, and record attachment metadata in the brief payload.
- [x] Run focused work/context tests; expect pass.
- [x] Commit `feat(work): attach graphtrail context to pre-edit briefs`.

### Task 3: Final verification

- [ ] Run through Brigade with `PY` pointing at the existing development venv: `./scripts/verify`.
- [ ] Generate two temporary verification receipts and assert the second stays below 100 KB and parses with `jq`.
