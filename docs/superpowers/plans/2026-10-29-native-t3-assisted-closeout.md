# Native T3 Assisted Closeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove useful result retention and accountable assisted closeout for one accepted native child, using existing Brigade notes and T3 settlement.

**Architecture:** Deliver a runbook and a narrow integration proof of existing behavior. The parent follows the runbook through supported native tools, retains reviewed results in the existing work session and records operator consumption separately from any verified existing journal acknowledgement. Native execution through Brigade and automatic settlement remain disabled.

**Tech Stack:** Existing Python CLI, pytest, standard-library subprocess/JSON/hashlib, native T3 tools. No new dependency or product module.

**Spec:** `docs/phase-2046-native-closeout-design.md`, approved 2026-10-29. Read the entire spec before this plan.

## Global Constraints

- Reuse native discovery, task control, notifications, worktrees and settlement. No scheduler, thread manager, service, recurring watcher or automatic fallback.
- Work only with an original accepted exact task/environment/thread handle. Missing or uncertain acceptance must not cause adoption or relaunch.
- Retain a reviewed result summary bounded to 8192 UTF-8 bytes. Label excerpts and preserve source/normalized digest distinctions. No raw transcripts, credentials or private memories.
- Operator consumption is not journal acknowledgement. Use an existing exact result ID only after verifying the association. Never invent a governed task to represent external work.
- Require fresh terminal/descendant/containment evidence and resolved or accepted child obligations. T3 read-check-settle-read is not atomic against new work.
- No product source, persisted schema, existing test assertion, shared backend, admission, budget, permission, roster or global policy changes.
- Use temporary targets for proof commands. No init, operator or quickstart against home or this checkout.
- Run verification through Brigade. Audit-only receipts and pending indexing remain explicit. Do not add a verifier manifest merely to score the work.
- Governed execution remains held pending named native enforcement and admission-backend owner acceptance. This proof cannot supply those assignments.

## Review Focus

1. A fresh reader must retrieve the actual retained summary, not just a shortened CLI preview. Task 1 checks `session.json` and `notes.md` through a fresh process.
2. Work notes strip outer whitespace, and reviewed/redacted output can differ from source bytes. Task 1 proves the two representations remain distinguished.
3. Ending a session does not automatically copy accumulated notes into its handoff. Task 1 proves an explicit closing reference leads to the retained result after session end.
4. Existing exact-result acknowledgement must never be represented by a fabricated task or a prose acceptance label. Task 1 reuses the existing task tests and documents the operator-only path.
5. New work or lost access near settlement must remain unresolved. Task 2 records the manual fixture dispositions and treats unknown live race semantics as a qualification gap, not a test success.

---

## Execution route and preflight

The user requested Codex workers and Astra for consequential review. On 2026-10-29 the user clarified the governing route: use T3 Code for supported harnesses, and `brigade run` only when T3 Code does not support the harness. Preserve that selection within actually callable routes and retain Brigade graph checks, verification receipts, ownership and review. Older shared-file wording must not reverse this current user direction. The repository's no-seat-credentials warning is scoped to Cursor Cloud.

At planning time, the local roster has `agents.sol61` with `cli = "codex"`, model `gpt-6.1-sol`. That does not establish T3-backed execution or live admission. The user separately authorized native T3/Codex Sol for this slice before giving the general routing clarification. Use the supported native Codex child route for this in-turn task. Use native T3 delegation when background work, another provider or an app-owned separate checkout is required. Never substitute direct provider exec after a T3 refusal.

- [ ] Read current instructions, roster, `brigade work brief --target .`, supported fleet status and claims. Keep one parent owner and do not duplicate a live claim.
- [ ] Confirm the selected T3/Codex model and applicable invocation contract. Preserve the exact accepted handle. An exposed catalog or dry plan is not dispatch success.
- [ ] If T3 cannot support the requested harness, inspect the approved Brigade-run seat and its live requirements. A temporary T3 failure does not itself authorize an unrequested provider or route switch. Do not edit roster or policy to remove a refusal.
- [ ] Use one explicitly selected Sol worker for this writing/proof unit. Preserve graph context, immutable source, artifacts and returned handles. No planner fan-out is needed. Never create a T3 child's checkout in a shell or prompt.

## Task 1: Runbook and existing-storage integration proof

**Files:**
- Create: `docs/runbooks/native-t3-assisted-closeout.md`
- Create: `tests/test_native_t3_closeout_proof.py`
- Reference unchanged: `src/brigade/native_t3_observations.py`, `src/brigade/work_cmd/session/lifecycle.py`, `src/brigade/run_tasks.py`
- Existing verification: `tests/test_native_t3_observations.py`, `tests/test_work_cmd_session.py`, `tests/test_run_tasks.py`

**Interfaces:**
- Consumes: approved protocol, `NativeTaskReference`, `classify_native_snapshot`, existing `work_cmd.start/note/end` and their actual CLI forms.
- Produces: a human-followed runbook and synthetic retained-note proof. No new public function, native controller, settlement evaluator or policy simulator.

- [ ] **Step 1: Record exact impact and custody.** Inspect `brigade code affected/impact/callers --help`, synchronize if stale and query the exact referenced paths. Record actual affected tests or concrete missing graph coverage before adding tests. Confirm the two new paths are outside other owners' write sets.
- [ ] **Step 2: Add the integration proof.** Use fake identities and `tmp_path`. Follow the existing session-test setup, isolate environment identities and stub only incidental session snapshot collection. Keep real note serialization and file I/O. Add these tests:
  - `test_reviewed_native_result_is_readable_in_fresh_process`: classify one supplied terminal snapshot, retain a reviewed note through the existing note API, then use `sys.executable` in a separate process to read `session.json` and `notes.md`. Assert exact accepted task/environment/thread, terminal-run/transfer refs, reviewed summary and explicit operator-only consumption survive. No active native tool or journal mutation occurs.
  - `test_source_and_retained_summary_have_distinct_digests`: supply synthetic source text and a shorter reviewed summary with non-ASCII content. Compute SHA-256 over each actual UTF-8 representation independently. Retain both labeled digests and verify the readback matches the retained normalized summary. This proves representation distinction, not automatic redaction or cap enforcement.
  - `test_ended_session_handoff_leads_to_retained_native_result`: end the temporary session with an explicit result/session reference and temporary handoff inbox. A fresh process must follow that reference to the summary and unchanged unresolved settlement state. Assert session end does not imply settlement or memory promotion.
- [ ] **Step 3: Run the smallest meaningful proof through Brigade.** Use the focused gate for `tests/test_native_t3_closeout_proof.py`. These are new integration checks of existing behavior and can pass immediately. Do not manufacture a failing product requirement or add a controller to obtain a RED. Any real unexpected failure requires diagnosis, not weakened assertions or out-of-scope production edits.
- [ ] **Step 4: Write the runbook.** Include all seven approved protocol steps, a fake filled result-note example and a blank checklist. Use the inspected CLI forms: `brigade work start --target <temporary-target> <title>`, `brigade work note --target <target> <one-argument-reviewed-text>`, `brigade work end --target <temporary-target> --note <explicit-reference> --handoff --handoff-inbox <temporary-inbox>`. Use structured subprocess arguments in examples that contain multiline content. Do not claim `work show` displays the full summary, because session display shortens note previews. Document reading retained note files after resolving the exact session path.
- [ ] **Step 5: Document the manual fixture workbook.** Cover every proof-table row in the spec using synthetic supplied snapshots and recorded proposed actions. Reuse existing classifier/task tests for identity refusal, pending work, immutable terminal results and acknowledgement. Document thread-read conflicts, incomplete summary, 8193-byte or multibyte-over-limit summary, live race, offline and obligation-transfer cases as manual review gates. Do not implement a test-only rule engine or claim a pretend settle proves actual native behavior.
- [ ] **Step 6: Run the required focused gate through Brigade:**

```bash
PATH="$PWD/.venv/ci312/bin:$PATH" PY="$PWD/.venv/ci312/bin" PYTHONPATH="$PWD/src" .venv/ci312/bin/brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_native_t3_closeout_proof.py","tests/test_native_t3_observations.py","tests/test_work_cmd_session.py","tests/test_run_tasks.py"]' --capture brigade-work
```

Require exit 0, complete receipt/log inspection and actual counts. Report attribution/indexing warnings separately. Existing assertions stay unchanged.

- [ ] **Step 7: Commit only the runbook and new proof tests.** Use a conventional commit and Codex co-author trailer. Return exact commit, changed paths, receipts and limitations. No push/PR/merge or native settlement by this worker. The parent retains review and acceptance ownership.

## Task 2: Parent review and bounded installed acceptance

**Files:**
- Create ignored evidence: `docs/plans/epic-2046/t3-native-integration-coordination/assisted-closeout-acceptance-20261029.md`
- Record durable findings in `.claude/memory-handoffs/` using its actual template.
- No production source or remote thread is changed merely to fill a test case.

**Interfaces:**
- Consumes: Task 1 commit, complete proof evidence, exact returned task handles and the runbook.
- Produces: independent review, synthetic-case dispositions and either a bounded installed acceptance record or an explicit held record. The parent keeps epic ownership.

- [ ] **Step 1: Review the exact worker diff and proof logs.** Obtain one fresh Astra review of the whole slice. Confirm no new product adapter, mock-only authority claim, weakened assertion or custody expansion. Fix small verified findings directly under current T3 review rules and reverify only affected checks.
- [ ] **Step 2: Complete the manual synthetic workbook.** Record each supplied fake case, the actual classifier/result-note evidence where applicable, the parent's disposition and its limits. Separate mechanically exercised behavior from human checklist review.
- [ ] **Step 3: Select at most one real eligible independent child already doing authorized work under this parent.** Do not start a dummy provider task, settle unrelated/old threads by title or sweep the sidebar. If none exists, record live acceptance held and continue source/PR work without claiming installed qualification.
- [ ] **Step 4: For an eligible child, follow the runbook through supported tools.** Preserve the original accepted exact handle, inspect actual result/evidence, retain/read back the reviewed note, record consumption and accepted obligations, reread terminal/descendant/thread state, then settle/read back the exact target. Any unresolved access, containment or new-work race stops or reopens closeout. No automatic retry, archive, delete, relaunch or implied authority.
- [ ] **Step 5: Capture real same-viewport sidebar before/after and a nearby unchanged row when installed acceptance is possible.** For a PR with rendered documentation, follow the current screenshot rule for base/head rendered docs, desktop/mobile where applicable. Use only authorized browser/UI tools and scrub screenshots before sharing. A missing screenshot remains a readiness gap, never a synthetic image presented as proof.
- [ ] **Step 6: Verify retained summary/evidence is readable after settlement from a fresh process.** State the observed timestamp and retain failures distinctly. Do not call readback an atomic guarantee, note consumption a journal acknowledgement, or handoff creation canonical ingestion.

## PR ownership and terminal state

- [ ] After the plan's review and implementation prerequisites, own the assigned in-house PR through required verification, current-head code/security reviews, actual bot findings, CI and protected merge. No release, deployment or native launch activation.
- [ ] Run the full gate once through Brigade for the PR, with `--timeout 3600` and bounded `BRIGADE_VERIFY_WAIT=60`. Do not use `--no-reuse` or retry status 75 with a larger timeout. CI-only checks remain required.
- [ ] Apply actual review labels/triggers after inspecting current configuration. No duplicate security scan, paid review or quota bypass. Link a created PR immediately, watch only when waiting on real checks/reviews and remove the watch on handback.
- [ ] Source/proof merge and live qualification are separate facts. If the real-child acceptance is held, label that gap in the PR and epic record and leave the epic milestone open. Native execution enablement remains held regardless of fixture results.
- [ ] Write the memory handoff and update the epic record with exact merged commit or the concrete current-head blocker. Do not close all of milestone 2 or the epic based on this assisted proof.

## Self-review

Spec coverage: Task 1 covers retained consumer, normalization, explicit journal distinction and the full manual decision workbook. Task 2 covers real identity/containment, result consumption, native settlement/readback, screenshots, restart/readback and explicit held states. Execution route is a separate preflight gate, not an inferred runtime capability.

Scope is two new implementation paths, the already-approved design and this plan. No new API or product persistence is needed. Automated integration assertions target storage/retrieval across processes, while existing focused selectors cover the reused task/classifier semantics. Human protocol choices and native race semantics are explicitly not described as automated enforcement.
