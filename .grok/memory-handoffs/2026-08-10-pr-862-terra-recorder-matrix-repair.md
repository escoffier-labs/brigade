# Memory Handoff

## Type

project-context

## Title

PR #862 Terra recorder-to-matrix contract repair

## Summary

Repair docs and add a recorder-to-matrix contract regression so #862 no longer implies a closed raw-capture allowlist for Codex app-server notifications.

## Durable facts

- codex_appserver forwards every non-delta notification; aboyeur records raw; deltas are salvage-only
- classification_matrix methods are scrub/export only; unknown methods remain unclassified and fail scrub/export
- docs/worker-event-streams.md now has Raw capture vs scrub matrix; section retitled Methods classified for scrubbed projection

## Evidence

- files changed: docs/worker-event-streams.md, src/brigade/worker_events.py, tests/test_worker_events.py, CHANGELOG.md
- commands run: brigade work verify run 20260810-221823-work-verify-1ff7bb; ./scripts/verify

## Recommended memory action

no-card

## Target document

.learnings/FEATURE_REQUESTS.md

## Suggested document content

### Terra repair on PR 862

Document that raw Codex app-server capture is open-ended for non-delta
methods while the classification matrix remains the closed scrub/export
contract. Unknown methods stay local/unclassified and are rejected from
scrub/export. Recorder behavior was intentionally not narrowed.
