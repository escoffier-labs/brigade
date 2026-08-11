# Memory Handoff

## Type

bugfix

## Title

Nested worker-event JSONL export boundary repair (#866 / #592)

## Summary

Terra sendback on PR #866 showed `events/nested/coder.jsonl` bypassed fail-closed export and was classified public support. Discovery is now recursive under `events/`, nested streams scrub to sidecars or refuse export, and validate/import reject nested raw-stream smuggling.

## Durable facts

- `list_worker_event_streams` uses `rglob("*.jsonl")` and excludes only top-level `events/lifecycle.jsonl`
- `is_raw_worker_event_stream_rel` / `is_scrubbed_worker_event_stream_rel` accept nested `events/**` paths
- Nested scrubbed sidecars land beside the raw path as `<stem>.scrubbed.json`
- Validate/import refuse nested public-support smuggling of raw worker JSONL
- Source-run raw streams remain local/`unclassified` under `policy=local-only`

## Evidence

- files changed: `src/brigade/worker_events.py`, `tests/test_work_run_archive.py`, `docs/worker-event-streams.md`, `docs/receipt-schemas.md`, `CHANGELOG.md`
- commands run: `brigade code impact list_worker_event_streams`; `brigade work verify run ...`; `pytest -q tests/test_work_run_archive.py tests/test_worker_events.py`
- error strings: `raw worker stream crossed an export boundary: events/nested/coder.jsonl`; `export refuses raw worker stream events/nested/coder.jsonl`

## Recommended memory action

no-card

## Target document

.learnings/ERRORS.md

## Suggested document content

### Nested worker JSONL export bypass

Worker-like JSONL anywhere under `events/` must be treated as a scrub/export
candidate. Top-level-only glob left nested streams classified as public
support; recursive discovery plus validate/import refusal closes that hole.
