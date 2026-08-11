# Memory Handoff

## Type

workflow

## Title

PR #866 rebase onto main 3532f07c for landing

## Summary

Landing follow-up on PR #866 rebased the reviewed #592 export privacy branch onto exact current main `3532f07cb125bfb6ad5c06a3fab0eb97ee555aeb`. The only main delta was evidence-ledger E2 (#865); CHANGELOG.md and docs/receipt-schemas.md needed no conflict resolution because those files did not overlap with main. Reviewed #592 behavior stayed byte-identical for archive/worker-event sources.

## Durable facts

- Exact main tip for this landing was `3532f07cb125bfb6ad5c06a3fab0eb97ee555aeb`
- Rebase of the three #592 commits was clean with no semantic conflicts
- Expected mechanical CHANGELOG.md / docs/receipt-schemas.md overlap did not appear against that main tip
- Reviewed archive/worker-event source trees matched pre-rebase tip `e48b4af6`

## Evidence

- files changed: rebase only (no #592 source edits)
- commands run: `git rebase 3532f07cb125bfb6ad5c06a3fab0eb97ee555aeb`; `pytest -q tests/test_work_run_archive.py tests/test_worker_events.py`; `./scripts/verify`
- error strings: none

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

### PR #866 landing rebase

When rebasing #866 onto main `3532f07c`, expect a clean replay if main only
carries evidence-ledger E2. Preserve the three #592 commits unchanged; do not
invent CHANGELOG/receipt-schemas merges unless a real conflict appears.
