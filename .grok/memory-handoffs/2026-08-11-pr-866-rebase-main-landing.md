# Memory Handoff

## Type

workflow

## Title

PR #866 rebase onto main 3e9278f9 for landing

## Summary

Landing follow-up on PR #866 rebased the reviewed #592 export privacy branch onto exact current main `3e9278f9f3c968fc95783207d4ed040bc0bfb672` (includes merged #863). Prior tip `3532f07c` was superseded by #863 onboarding/gitignore work. Replay stayed clean: CHANGELOG.md absorbed main's #860/#863 Fixed and Documentation bullets beside the #592 Added entry without a conflict marker, and docs/receipt-schemas.md did not overlap. Reviewed #592 archive/worker-event sources stayed byte-identical.

## Durable facts

- Exact main tip for this landing is `3e9278f9f3c968fc95783207d4ed040bc0bfb672`
- Rebase of the #592 commits plus landing handoff was clean with no semantic conflicts
- CHANGELOG.md carries both main #860/#863 entries and the #592 worker-event privacy bullet
- docs/receipt-schemas.md needed no conflict resolution against that tip
- Reviewed archive/worker-event source trees matched pre-rebase tip `f2236f15`

## Evidence

- files changed: rebase only (no #592 source edits)
- commands run: `git rebase 3e9278f9f3c968fc95783207d4ed040bc0bfb672`; `pytest -q tests/test_work_run_archive.py tests/test_worker_events.py`; `./scripts/verify`
- error strings: none

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

### PR #866 landing rebase onto 3e9278f9

When rebasing #866 onto main `3e9278f9` (merged #863), expect a clean replay
that keeps main's gitignore/quickstart CHANGELOG bullets and the #592 Added
entry together. Preserve the #592 commits unchanged; stop if any semantic
conflict appears outside mechanical CHANGELOG/docs overlap.
