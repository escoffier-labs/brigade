# Memory Handoff

## Type

bugfix

## Title

Reject malformed patrol fence history before applying actions

## Summary

Issue #1955 allowed valid JSON with malformed action status, scope, or idempotency keys to pass the receipt reader. Fence compaction could then discard the record or synthesize a key, permitting a repeated action. The shared reader now rejects malformed fence records before retention or apply consumes them.

## Durable facts

- All five fence statuses require nonblank string scope and idempotency keys. Unknown or mistyped statuses are corrupt history.
- Valid refused/skipped outcomes remain readable without fence keys, and optional legacy lane metadata remains optional.
- The same reader protects ordinary receipts, the legacy archive, and lane-specific archives. Apply-boundary tests preserve both the ledger claim and corrupt evidence on refusal.
- Full-suite failures must be distinguished from focused success. All 157 failures from this change's full gate reproduced on untouched base `a3e3ade50ad337847721e849f5174b4629ef36ba`; none were patrol tests.

## Evidence

- files changed: `src/brigade/runs_patrol_receipts.py`, `tests/test_runs_patrol_receipts.py`, `CHANGELOG.md`
- initial regression run before the fix: 39 failed.
- focused command: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_runs_patrol_receipts.py","tests/test_runs_patrol_apply.py"]' --capture brigade-work`; 185 passed, including a repeat with supported Ruff 0.17.2.
- full gate receipt: `20261026-102029-work-verify-a56020`; `Aggregate: 47350 passed, 157 failed, 209 skipped, 0 errors`. This was not a full-suite pass.
- untouched-base reproduction receipt: `20261026-104429-work-verify-0f382b`; all 157 selected failures reproduced with matching pytest/runtime dependencies.
- refusal: `invalid patrol action history`

## Recommended memory action

create-card

## Target card

patrol-action-history.md

## Suggested card content

```markdown
---
topic: patrol-action-history
category: project-context
tags: [patrol, receipts, idempotency]
---

# Patrol action history

Patrol reads ordinary receipts and both archive formats through one strict reader. Fence-bearing records require a known status and nonblank string scope/idempotency keys; corrupt history must refuse apply rather than drop a retry fence. Legacy refused/skipped outcomes are not fences and may omit those keys. Optional legacy lane metadata remains optional.
```
