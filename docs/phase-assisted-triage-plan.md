# Assisted diagnosis fixture Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for bounded implementation and review.

**Goal:** Prove existing bug-report and work-import controls compose for reviewed
Brigade diagnoses, including memory visibility and MCP drift symptoms.

**Architecture:** One integration test module uses temp repositories and isolated
report state. Recorded assistance is input data. Existing CLI entry points own
redaction, consent, canonical identity, inbox writes and task promotion.

**Tech Stack:** Python, pytest and existing Brigade CLI. No new dependency.

**Spec:** `docs/phase-assisted-triage.md`.

## Global Constraints

- No live triage, memory or MCP investigation. No native adapter edits.
- No external issue publication. Mock the existing bug-report transport.
- No change to proof/authentication requirements to make dedup pass.
- No production implementation in this slice. Report any missing seam with evidence.
- Preserve other work. Parent owns docs and handoff. Worker owns the test module.

## demi

Actual ask: diagnose Brigade symptoms and offer approved issue filing for code bugs.
Smallest useful slice: executable proof that existing report and inbox controls
carry reviewed evidence safely through both configuration and code-defect cases.
Highest rung that holds: existing commands and test fixtures.
Existing patterns: `tests/test_bug_report.py` and work-import provenance tests.
Growth trigger: proof identifies a missing behavior, or a later bounded evidence
collector and user-facing invocation receive concrete design alignment.

## Review Focus

- Temp target isolation must also isolate global last-error/install-state readers.
- Memory and MCP case text cannot assert real loaded state from fixture declarations.
- Metadata-only changes must not be mistaken for canonical content changes.
- Untrusted verification metadata must not create a passing receipt.
- Issue submission requires the exact saved preview and existing consent controls.

## Task 1: Compose the existing controls

**Files:** Create `tests/test_assisted_triage_workflow.py` only.
**Interfaces:** Use `brigade.cli.main(argv)` and existing temp/env fixture patterns.
No new public functions or schemas. Return observations to the parent.

- [ ] Refresh graph and record affected source and tests before editing. Initial
  six seams and depth-3 graph result are in the design. Query the new test path
  and relevant existing seams. Missing new-file coverage is explicitly expected.
- [ ] Create isolated temp Git repos and report data roots. Deny networking and
  mock all publication transports. Strip credentials and isolate host readers.
- [ ] Exercise report preparation for memory-access mismatch, MCP drift and a
  suspected code bug. Use recorded reviewed diagnoses, clearly fixture inputs.
  Check the actual report projection and redaction across the composed workflow.
- [ ] Exercise validate, dry-run, ingest and repeat ingest. Assert no preview
  inbox writes, importer-owned identity, proof-appropriate dedup, changed digest
  as content, pending-only records and unchanged task/verification state.
- [ ] Exercise invalid input, hostile source/identity/provenance and untrusted
  passing-verification claims. Do not assert metadata is universally removed.
- [ ] Exercise the code-bug report's saved-preview consent boundary using fake
  issue transport. No network or real issue may be created. Reuse existing
  regressions for exact-preview integrity to keep this suite focused on composition.
- [ ] Run focused verification through Brigade:
  `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_assisted_triage_workflow.py"]' --capture brigade-work`.
  Report the observed verdict and any failures verbatim. Never weaken a failing test.

## Task 2: Owner review and PR gates

- [ ] Review the diff against scope, privacy, proof posture and test value.
- [ ] Run a fresh bounded independent code review, fix verified findings.
- [ ] Update design/handoff with observed proof and remaining product gap.
- [ ] Before a PR run the full gate once through Brigade, then content/writing
  checks, authorized commit/push and linked in-house PR. Inspect effective review
  triggers and current-head CI. Use the protected merge route only when eligible.

## Execution ruling

The user approved the fixture design and requested memory/MCP symptom coverage
and issue filing. The existing native T3 worker exception supplies the execution
method. Proceed with a bounded Sol worker without another routine plan approval.
This plan does not represent a shipped automatic diagnosis command.
