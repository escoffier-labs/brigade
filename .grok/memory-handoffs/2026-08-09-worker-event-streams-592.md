# Memory Handoff

## Type

project-context

## Title

Worker event stream classification and fail-closed scrubbing (#592)

## Summary

Issue #592 first slice adds a closed field-classification matrix and fail-closed scrubber for Codex app-server worker streams under `events/<worker>.jsonl`. Scrubbed projections keep public metadata, stable IDs, safe operation names, schema versions, and source digests; private/secret/prohibited fields are omitted. Audit/replay reject raw streams unless `policy=local-only`; resume uses local-only. Legacy raw NDJSON stays inspectable and marked unclassified until scrubbed.

## Durable facts

- Module: `src/brigade/worker_events.py`; docs: `docs/worker-event-streams.md`; goldens: `src/brigade/fixtures/worker-events-appserver.v1.golden.json`
- Media types: raw `application/vnd.brigade.worker-events.appserver+jsonl` vs scrubbed `...scrubbed+jsonl`
- Consumer APIs: `scrub_event` / `scrub_stream_file`, `inspect_stream_file`, `load_stream_for_consumer`, `run_audit.reject_raw_worker_stream_evidence`
- First slice excludes legacy backfill and archive export wiring; unknown methods/fields fail closed with bounded diagnostics

## Evidence

- files changed: `src/brigade/worker_events.py`, `src/brigade/run_audit.py`, `src/brigade/run_resume.py`, `tests/test_worker_events.py`, `docs/worker-event-streams.md`, `CHANGELOG.md`, golden fixture
- commands run: `pytest -q tests/test_worker_events.py`, `./scripts/verify`
- error strings: `rejects unclassified worker event stream`; `unknown event method`; `unknown field`

## Recommended memory action

no-card

## Target document

.learnings/FEATURE_REQUESTS.md

## Suggested document content

Worker event scrubbing (#592) landed as library-first: no new CLI. Follow-ups are scrubbed sidecar write path, archive role/media-type wiring, and enabling `worker_event_streams` audit coverage once fixtures are scrubbed.
