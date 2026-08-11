# Memory Handoff

## Type

bugfix

## Title

Lifecycle journal reserved-path worker-envelope smuggling (#866)

## Summary

Maintainer review on PR #866 found that reserved top-level `events/lifecycle.jsonl` was excluded from worker-stream discovery without authenticating its record shape, so a digest-valid archive could smuggle raw JSON-RPC worker notifications as public `role=journal`. Export/validate/import now authenticate `brigade.run_event.v1` journals and reject worker envelopes in that path.

## Durable facts

- Nested worker-stream recursive discovery remains in place
- `assert_lifecycle_journal_authentic` rejects JSON-RPC worker envelopes in `events/lifecycle.jsonl`
- Valid empty or `brigade.run_event.v1` lifecycle journals still export/validate/import
- `_assert_export_privacy_tree` runs checkpoint, worker-stream, and lifecycle gates together

## Evidence

- files changed: `src/brigade/work_run_archive.py`, `tests/test_work_run_archive.py`, `docs/worker-event-streams.md`, `docs/receipt-schemas.md`, `CHANGELOG.md`
- commands run: `pytest -q tests/test_work_run_archive.py tests/test_worker_events.py`; `brigade work verify run`; `./scripts/verify`
- error strings: `events/lifecycle.jsonl carries a worker notification envelope; reserved journal path rejects raw JSON-RPC worker streams`

## Recommended memory action

no-card

## Target document

.learnings/ERRORS.md

## Suggested document content

### Lifecycle journal reserved-path smuggle

Excluding `events/lifecycle.jsonl` from worker discovery is not enough. Archive
export/validate/import must authenticate the lifecycle record shape or reject
JSON-RPC worker envelopes before granting the public journal classification.
